#!/usr/bin/env python3
"""
Knowledge Graph ingestion pipeline using neo4j-graphrag SimpleKGPipeline.

Reads Zoom meeting data, transcripts, summaries, and attendee info from
/workspace/kg_export/ and feeds it through SimpleKGPipeline for entity/relation
extraction into Neo4j.
"""

import asyncio
import json
import glob
import os
import sys
import time
import traceback
from pathlib import Path
from dotenv import load_dotenv

import neo4j
from neo4j_graphrag.llm import OpenAILLM
from neo4j_graphrag.embeddings import OpenAIEmbeddings
from neo4j_graphrag.experimental.pipeline.kg_builder import SimpleKGPipeline
from neo4j_graphrag.experimental.components.text_splitters.fixed_size_splitter import FixedSizeSplitter

load_dotenv()

# --- Configuration ---
DATA_DIR = Path("/workspace/kg_export/data")
FILES_DIR = Path("/workspace/kg_export/files")
BATCH_SIZE = 50  # Start with 50 meetings

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://host.docker.internal:7691")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "kg-eval-password")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Entity and relationship schema for the domain
NODE_TYPES = [
    "Person", "Client", "Meeting", "ActionItem", "Decision",
    "Campaign", "MarketingChannel", "Topic", "Organization", "Department"
]

RELATIONSHIP_TYPES = [
    "ATTENDED", "ABOUT", "PRODUCED", "ASSIGNED_TO",
    "LED_TO", "AFFECTS", "WORKS_FOR", "DISCUSSED",
    "FOLLOWED_UP", "MENTIONED", "HOSTED", "IN_DEPARTMENT"
]

PATTERNS = [
    ("Person", "ATTENDED", "Meeting"),
    ("Person", "HOSTED", "Meeting"),
    ("Meeting", "ABOUT", "Client"),
    ("Meeting", "PRODUCED", "ActionItem"),
    ("ActionItem", "ASSIGNED_TO", "Person"),
    ("Meeting", "LED_TO", "Decision"),
    ("Decision", "AFFECTS", "Campaign"),
    ("Person", "WORKS_FOR", "Organization"),
    ("Person", "IN_DEPARTMENT", "Department"),
    ("Meeting", "DISCUSSED", "Topic"),
    ("Meeting", "FOLLOWED_UP", "Meeting"),
    ("Person", "MENTIONED", "Campaign"),
]


def load_json(path):
    """Load a JSON file."""
    with open(path, "r") as f:
        return json.load(f)


def build_lookup_tables():
    """Build lookup tables from exported data."""
    print("Loading data files...")

    # Meetings
    meetings = load_json(DATA_DIR / "zoom_meetings.json")
    meetings_by_uuid = {m["uuid"]: m for m in meetings}
    meetings_by_id = {}
    for m in meetings:
        mid = str(m["id"])
        if mid not in meetings_by_id:
            meetings_by_id[mid] = []
        meetings_by_id[mid].append(m)
    print(f"  Loaded {len(meetings)} meetings")

    # Attendees
    attendees = load_json(DATA_DIR / "zoom_past_meeting_attendees.json")
    attendees_by_uuid = {}
    for a in attendees:
        uuid = a["meeting_uuid"]
        if uuid not in attendees_by_uuid:
            attendees_by_uuid[uuid] = []
        attendees_by_uuid[uuid].append(a["email"])
    print(f"  Loaded {len(attendees)} attendee records")

    # Clients
    clients = load_json(DATA_DIR / "clients.json")
    clients_by_hubspot = {c["hubspot_id"]: c for c in clients if c.get("hubspot_id")}
    print(f"  Loaded {len(clients)} clients")

    # Employees
    employees = load_json(DATA_DIR / "airtable_users.json")
    employees_by_email = {e["email"]: e for e in employees if e.get("email")}
    print(f"  Loaded {len(employees)} employees")

    # Recording summaries
    rec_summaries = load_json(DATA_DIR / "zoom_recording_summaries.json")
    summaries_by_uuid = {s["meeting_uuid"]: s for s in rec_summaries if s.get("meeting_uuid")}
    summaries_by_id = {}
    for s in rec_summaries:
        mid = str(s.get("meeting_id", ""))
        if mid not in summaries_by_id:
            summaries_by_id[mid] = []
        summaries_by_id[mid].append(s)
    print(f"  Loaded {len(rec_summaries)} recording summaries")

    # Analysis results
    analyses = load_json(DATA_DIR / "analysis_results.json")
    analyses_by_uuid = {}
    for a in analyses:
        uuid = a.get("meeting_uuid")
        if uuid:
            if uuid not in analyses_by_uuid:
                analyses_by_uuid[uuid] = []
            analyses_by_uuid[uuid].append(a)
    print(f"  Loaded {len(analyses)} analysis results")

    return {
        "meetings": meetings,
        "meetings_by_uuid": meetings_by_uuid,
        "meetings_by_id": meetings_by_id,
        "attendees_by_uuid": attendees_by_uuid,
        "clients_by_hubspot": clients_by_hubspot,
        "employees_by_email": employees_by_email,
        "summaries_by_uuid": summaries_by_uuid,
        "summaries_by_id": summaries_by_id,
        "analyses_by_uuid": analyses_by_uuid,
    }


def find_meeting_files(meeting_id):
    """Find all files for a given meeting ID in the files directory."""
    pattern = str(FILES_DIR / f"{meeting_id}_*")
    files = glob.glob(pattern)
    result = {
        "instance": [],
        "transcript": [],
        "summary": [],
        "next_steps": [],
        "timeline": [],
    }
    for f in files:
        basename = os.path.basename(f)
        if basename.endswith("_instance.json"):
            result["instance"].append(f)
        elif "_audio_transcript_" in basename and basename.endswith(".VTT"):
            result["transcript"].append(f)
        elif "_summary_next_steps_" in basename:
            result["next_steps"].append(f)
        elif "_summary_" in basename and basename.endswith(".JSON"):
            result["summary"].append(f)
        elif "_timeline_" in basename:
            result["timeline"].append(f)
    return result


def read_vtt_transcript(path):
    """Read a VTT transcript file and extract plain text."""
    lines = []
    with open(path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            # Skip WEBVTT header, timestamps, sequence numbers, empty lines
            if (line == "WEBVTT" or
                line == "" or
                "-->" in line or
                line.isdigit()):
                continue
            lines.append(line)
    return "\n".join(lines)


def read_json_safe(path):
    """Safely read a JSON file."""
    try:
        with open(path, "r", errors="replace") as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        print(f"  Warning: Could not parse {path}: {e}")
        return None


def build_meeting_document(meeting, lookups):
    """Build a text document for a meeting combining all available data."""
    meeting_id = str(meeting["id"])
    meeting_uuid = meeting["uuid"]
    topic = meeting.get("topic", "Unknown")
    host_email = meeting.get("host_email", "Unknown")
    hubspot_id = meeting.get("hubspot_id")

    # Resolve client
    client_name = "Unknown"
    client_code = ""
    if hubspot_id and hubspot_id in lookups["clients_by_hubspot"]:
        client = lookups["clients_by_hubspot"][hubspot_id]
        client_name = client.get("internal_client_name") or client.get("client_name", "Unknown")
        client_code = client.get("client_code", "")

    # Resolve attendees
    attendee_emails = lookups["attendees_by_uuid"].get(meeting_uuid, [])
    attendee_names = []
    for email in attendee_emails:
        emp = lookups["employees_by_email"].get(email)
        if emp:
            name = emp.get("name", email)
            dept_raw = emp.get("service_department", "[]")
            try:
                dept = json.loads(dept_raw) if isinstance(dept_raw, str) else dept_raw
                dept_str = dept[0] if dept else ""
            except (json.JSONDecodeError, IndexError):
                dept_str = ""
            level_raw = emp.get("level", "[]")
            try:
                level = json.loads(level_raw) if isinstance(level_raw, str) else level_raw
                level_str = level[0] if level else ""
            except (json.JSONDecodeError, IndexError):
                level_str = ""
            attendee_names.append(f"{name} ({dept_str}, {level_str})")
        else:
            attendee_names.append(email)

    # Resolve host name
    host_emp = lookups["employees_by_email"].get(host_email)
    host_name = host_emp["name"] if host_emp else host_email

    # Build header
    parts = []
    parts.append(f"Meeting: {topic}")
    parts.append(f"Meeting ID: {meeting_id}")
    parts.append(f"Host: {host_name} ({host_email})")
    if client_name != "Unknown":
        parts.append(f"Client: {client_name} ({client_code})")
    if attendee_names:
        parts.append(f"Attendees: {', '.join(attendee_names[:20])}")
    if meeting.get("sentiment_score") is not None:
        parts.append(f"Sentiment Score: {meeting['sentiment_score']}")
    parts.append("")

    # Find meeting files
    files = find_meeting_files(meeting_id)

    # Add instance metadata (date, duration)
    for inst_path in files["instance"][:1]:
        inst = read_json_safe(inst_path)
        if inst:
            occ = inst.get("occurrence_info", {})
            parts.append(f"Date: {occ.get('start_time', 'Unknown')}")
            parts.append(f"Duration: {occ.get('duration', 'Unknown')} minutes")
            parts.append("")

    # Add summary from files
    for sum_path in files["summary"][:1]:
        summary_data = read_json_safe(sum_path)
        if summary_data:
            overall = summary_data.get("overall_summary", "")
            if overall:
                parts.append("## Meeting Summary")
                parts.append(overall)
                parts.append("")
            items = summary_data.get("items", [])
            for item in items:
                label = item.get("label", "")
                summary_text = item.get("summary", "")
                if label and summary_text:
                    parts.append(f"### {label}")
                    parts.append(summary_text)
                    parts.append("")

    # Add recording summary from database export
    db_summary = lookups["summaries_by_uuid"].get(meeting_uuid)
    if not db_summary:
        # Try by meeting_id
        db_summaries = lookups["summaries_by_id"].get(meeting_id, [])
        if db_summaries:
            db_summary = db_summaries[0]
    if db_summary:
        summary_text = db_summary.get("summary") or db_summary.get("original_summary") or ""
        if summary_text:
            parts.append("## Recording Summary")
            parts.append(summary_text)
            parts.append("")
        topics_raw = db_summary.get("summary_topics") or db_summary.get("original_summary_topics") or ""
        if topics_raw:
            try:
                topics = json.loads(topics_raw) if isinstance(topics_raw, str) else topics_raw
                if isinstance(topics, list):
                    parts.append("## Topics Discussed")
                    for t in topics:
                        if isinstance(t, dict):
                            parts.append(f"- {t.get('label', '')}: {t.get('summary', '')}")
                        else:
                            parts.append(f"- {t}")
                    parts.append("")
            except json.JSONDecodeError:
                pass
        next_steps = db_summary.get("next_steps") or db_summary.get("original_next_steps") or ""
        if next_steps:
            parts.append("## Next Steps")
            parts.append(next_steps)
            parts.append("")

    # Add next steps from files
    for ns_path in files["next_steps"][:1]:
        ns_data = read_json_safe(ns_path)
        if ns_data and isinstance(ns_data, dict):
            items = ns_data.get("items", [])
            if items:
                parts.append("## Action Items (from Zoom AI)")
                for item in items:
                    text = item.get("rephrased_text") or item.get("text", "")
                    assignees = item.get("assignees", [])
                    assignee_names_list = [a.get("username", "") for a in assignees if a.get("username")]
                    if text:
                        assignee_str = f" [Assigned to: {', '.join(assignee_names_list)}]" if assignee_names_list else ""
                        parts.append(f"- {text}{assignee_str}")
                parts.append("")

    # Add transcript (truncated to avoid token overload)
    for tr_path in files["transcript"][:1]:
        transcript_text = read_vtt_transcript(tr_path)
        if transcript_text:
            # Limit transcript to ~8000 chars to keep LLM costs manageable
            if len(transcript_text) > 8000:
                transcript_text = transcript_text[:8000] + "\n... [transcript truncated]"
            parts.append("## Transcript")
            parts.append(transcript_text)
            parts.append("")

    # Add analysis results
    analyses = lookups["analyses_by_uuid"].get(meeting_uuid, [])
    for analysis in analyses[:1]:  # Just first analysis
        output = analysis.get("analysis_output_markdown", "")
        if output:
            # Truncate long analyses
            if len(output) > 4000:
                output = output[:4000] + "\n... [analysis truncated]"
            parts.append("## AI Analysis")
            parts.append(output)
            parts.append("")

    full_text = "\n".join(parts)
    return full_text


def select_meetings_with_content(meetings, lookups, max_count=BATCH_SIZE):
    """Select meetings that have the most content (transcripts, summaries)."""
    scored = []
    for m in meetings:
        meeting_id = str(m["id"])
        meeting_uuid = m["uuid"]
        score = 0

        # Check for files
        files = find_meeting_files(meeting_id)
        if files["transcript"]:
            score += 3
        if files["summary"]:
            score += 2
        if files["next_steps"]:
            score += 1

        # Check for DB summary
        if meeting_uuid in lookups["summaries_by_uuid"]:
            s = lookups["summaries_by_uuid"][meeting_uuid]
            if s.get("summary") or s.get("original_summary"):
                score += 2

        # Check for analysis
        if meeting_uuid in lookups["analyses_by_uuid"]:
            score += 1

        # Check for attendees
        if meeting_uuid in lookups["attendees_by_uuid"]:
            score += 1

        # Check for client
        if m.get("hubspot_id") and m["hubspot_id"] in lookups["clients_by_hubspot"]:
            score += 1

        if score > 0:
            scored.append((score, m))

    # Sort by score descending, take top N
    scored.sort(key=lambda x: x[0], reverse=True)
    selected = [m for _, m in scored[:max_count]]
    print(f"\nSelected {len(selected)} meetings with content (from {len(meetings)} total)")
    if scored:
        print(f"  Score range: {scored[0][0]} to {scored[min(max_count-1, len(scored)-1)][0]}")
    return selected


async def run_pipeline():
    """Main pipeline execution."""
    start_time = time.time()

    # Load all data
    lookups = build_lookup_tables()

    # Select meetings with the most content
    selected_meetings = select_meetings_with_content(lookups["meetings"], lookups, BATCH_SIZE)

    # Connect to Neo4j
    print(f"\nConnecting to Neo4j at {NEO4J_URI}...")
    driver = neo4j.GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    try:
        driver.verify_connectivity()
        print("  Connected successfully!")
    except Exception as e:
        print(f"  Connection failed: {e}")
        print("  Continuing anyway - will fail on write...")

    # Set up LLM and embedder
    print("\nSetting up LLM (OpenAI GPT-4o-mini) and embedder (text-embedding-3-small)...")
    llm = OpenAILLM(
        model_name="gpt-4o-mini",
        model_params={
            "temperature": 0,
            "max_tokens": 4000,
        },
    )
    embedder = OpenAIEmbeddings(model="text-embedding-3-small")

    # Set up text splitter
    text_splitter = FixedSizeSplitter(chunk_size=2000, chunk_overlap=200)

    # Create pipeline
    print("Creating SimpleKGPipeline...")
    pipeline = SimpleKGPipeline(
        llm=llm,
        driver=driver,
        embedder=embedder,
        entities=NODE_TYPES,
        relations=RELATIONSHIP_TYPES,
        potential_schema=PATTERNS,
        from_pdf=False,
        text_splitter=text_splitter,
        perform_entity_resolution=True,
        on_error="IGNORE",
    )

    # Process meetings
    results = {
        "processed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
        "documents": [],
    }

    print(f"\nProcessing {len(selected_meetings)} meetings...\n")
    for i, meeting in enumerate(selected_meetings):
        meeting_id = str(meeting["id"])
        topic = meeting.get("topic", "Unknown")[:60]
        print(f"  [{i+1}/{len(selected_meetings)}] {topic} (ID: {meeting_id})")

        try:
            doc_text = build_meeting_document(meeting, lookups)
            if len(doc_text.strip()) < 100:
                print(f"    -> Skipped (too little content: {len(doc_text)} chars)")
                results["skipped"] += 1
                continue

            print(f"    -> Document: {len(doc_text)} chars")

            # Run pipeline
            result = await pipeline.run_async(
                text=doc_text,
                document_metadata={
                    "meeting_id": str(meeting_id),
                    "meeting_uuid": str(meeting.get("uuid", "")),
                    "topic": str(meeting.get("topic", "")),
                    "host_email": str(meeting.get("host_email", "")),
                    "hubspot_id": str(meeting.get("hubspot_id", "")),
                },
            )
            results["processed"] += 1
            results["documents"].append({
                "meeting_id": meeting_id,
                "topic": meeting.get("topic", ""),
                "chars": len(doc_text),
                "status": "ok",
            })
            print(f"    -> OK")

        except Exception as e:
            err_msg = str(e)[:200]
            print(f"    -> ERROR: {err_msg}")
            results["failed"] += 1
            results["errors"].append({
                "meeting_id": meeting_id,
                "topic": meeting.get("topic", ""),
                "error": str(e)[:500],
            })
            # If first 3 all fail, likely a systemic issue - bail
            if results["failed"] >= 3 and results["processed"] == 0:
                print("\n  !!! First 3 meetings all failed. Stopping to investigate. !!!")
                break

    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"Pipeline complete in {elapsed:.1f}s")
    print(f"  Processed: {results['processed']}")
    print(f"  Failed:    {results['failed']}")
    print(f"  Skipped:   {results['skipped']}")
    if results["errors"]:
        print(f"\nFirst few errors:")
        for err in results["errors"][:5]:
            print(f"  - {err['meeting_id']}: {err['error'][:100]}")

    # Save results
    results_path = Path("/workspace/kg-2/ingest_results.json")
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {results_path}")

    driver.close()
    return results


if __name__ == "__main__":
    results = asyncio.run(run_pipeline())

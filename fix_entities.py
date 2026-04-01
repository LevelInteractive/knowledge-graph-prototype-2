#!/usr/bin/env python3
"""
Entity resolution and cleanup script for Knowledge Graph Setup B (kg-2).

Fixes:
1. Person name resolution - merge first-name-only nodes with full-name nodes
2. Client misclassification cleanup
3. Phantom Meeting node cleanup
4. General dedup (Levenshtein distance <= 2)
5. Report before/after stats
"""

import json
import os
import re
from collections import defaultdict

import neo4j

# ── Connection ──────────────────────────────────────────────────────────────
NEO4J_URI = "bolt://host.docker.internal:7691"
NEO4J_USER = "neo4j"
NEO4J_PASS = "kg-eval-password"

# ── Data paths ──────────────────────────────────────────────────────────────
AIRTABLE_USERS = "/workspace/kg_export/data/airtable_users.json"
CLIENTS_FILE = "/workspace/kg_export/data/clients.json"
ATTENDEES_FILE = "/workspace/kg_export/data/zoom_past_meeting_attendees.json"


def load_json(path):
    with open(path) as f:
        return json.load(f)


def run_query(session, query, params=None):
    result = session.run(query, params or {})
    return [dict(r) for r in result]


def get_node_counts(session):
    """Return dict of label -> count."""
    rows = run_query(session, """
        MATCH (n)
        WITH labels(n) AS lbls
        UNWIND lbls AS label
        WITH label, count(*) AS cnt
        RETURN label, cnt ORDER BY cnt DESC
    """)
    return {r["label"]: r["cnt"] for r in rows}


# ═══════════════════════════════════════════════════════════════════════════
# 1. Person Name Resolution
# ═══════════════════════════════════════════════════════════════════════════

def fix_person_names(session):
    """Merge first-name-only Person nodes with their full-name counterparts."""
    print("\n" + "=" * 70)
    print(" STEP 1: Person Name Resolution")
    print("=" * 70)

    # Load employee list for reference
    employees = load_json(AIRTABLE_USERS)
    # Build lookup: first_name (lower) -> list of full names
    emp_by_first = defaultdict(list)
    for emp in employees:
        first = (emp.get("preferred_first_name") or emp.get("first_name") or "").strip().lower()
        full = emp.get("name", "").strip()
        if first and full:
            emp_by_first[first].append(full)

    # Also load attendees for email -> name mapping
    attendees = load_json(ATTENDEES_FILE)
    email_to_name = {}
    for att in attendees:
        email = att.get("email", "")
        if email and "@" in email:
            parts = email.split("@")[0].split(".")
            if len(parts) >= 2:
                name = f"{parts[0].capitalize()} {parts[1].capitalize()}"
                email_to_name[email] = name

    # Get all Person nodes from Neo4j
    all_persons = run_query(session, "MATCH (p:Person) RETURN id(p) AS id, p.name AS name")

    # Classify nodes
    first_only = []
    full_name_persons = []
    junk_persons = []
    email_persons = []

    # Patterns for junk
    junk_names = {"agency", "agency team", "client", "the client", "team"}
    email_pattern = re.compile(r'^[\w.+-]+@[\w.-]+\.\w+$')
    slash_pattern = re.compile(r'/')  # "Yvette's/Misty" type entries

    for p in all_persons:
        name = (p["name"] or "").strip()
        name_lower = name.lower()
        if not name:
            junk_persons.append(p)
        elif email_pattern.match(name):
            email_persons.append(p)
        elif name_lower in junk_names:
            junk_persons.append(p)
        elif slash_pattern.search(name) or "'" in name and "/" in name:
            junk_persons.append(p)
        elif " " not in name:
            first_only.append(p)
        else:
            full_name_persons.append(p)

    print(f"  Total Person nodes: {len(all_persons)}")
    print(f"  First-name-only: {len(first_only)}")
    print(f"  Full-name: {len(full_name_persons)}")
    print(f"  Email addresses: {len(email_persons)}")
    print(f"  Junk entries: {len(junk_persons)}")

    merged = 0
    deleted = 0

    # --- Sub-step 1a: Handle email-address person nodes ---
    for p in email_persons:
        email = p["name"].strip()
        if email in email_to_name:
            resolved_name = email_to_name[email]
            existing = run_query(session,
                "MATCH (p:Person {name: $name}) RETURN id(p) AS id",
                {"name": resolved_name})
            if existing:
                _merge_person_nodes(session, p["id"], existing[0]["id"], email, resolved_name)
                merged += 1
            else:
                run_query(session,
                    "MATCH (p) WHERE id(p) = $id SET p.name = $name",
                    {"id": p["id"], "name": resolved_name})
                merged += 1
                print(f"    Renamed email: '{email}' -> '{resolved_name}'")
        else:
            # Try to derive name from email
            parts = email.split("@")[0].split(".")
            if len(parts) >= 2:
                derived = f"{parts[0].capitalize()} {parts[1].capitalize()}"
                existing = run_query(session,
                    "MATCH (p:Person {name: $name}) RETURN id(p) AS id",
                    {"name": derived})
                if existing:
                    _merge_person_nodes(session, p["id"], existing[0]["id"], email, derived)
                    merged += 1
                else:
                    run_query(session,
                        "MATCH (p) WHERE id(p) = $id SET p.name = $name",
                        {"id": p["id"], "name": derived})
                    merged += 1
                    print(f"    Renamed email: '{email}' -> '{derived}'")
            else:
                _delete_entity_node(session, p["id"], email, "unresolvable email Person")
                deleted += 1

    # --- Sub-step 1b: Delete junk person nodes ---
    for p in junk_persons:
        _delete_entity_node(session, p["id"], p["name"] or "(empty)", "junk Person")
        deleted += 1

    # --- Sub-step 1c: Co-occurrence disambiguation for ambiguous cases ---
    # For first-name-only nodes with multiple candidate matches, check which
    # full-name person shares the most meetings (via ATTENDED) with the first-name node.
    graph_by_first = defaultdict(list)
    # Re-query full-name persons (some may have been added by email resolution)
    full_name_persons = run_query(session,
        "MATCH (p:Person) WHERE p.name CONTAINS ' ' RETURN id(p) AS id, p.name AS name")
    for p in full_name_persons:
        first = p["name"].split()[0].lower()
        graph_by_first[first].append(p)

    skipped_ambiguous = 0
    skipped_no_match = 0

    for p in first_only:
        first = p["name"].strip().lower()
        graph_candidates = graph_by_first.get(first, [])
        emp_candidates = emp_by_first.get(first, [])

        if len(graph_candidates) == 1:
            target = graph_candidates[0]
            _merge_person_nodes(session, p["id"], target["id"], p["name"], target["name"])
            merged += 1
        elif len(graph_candidates) == 0 and len(emp_candidates) == 1:
            new_name = emp_candidates[0]
            existing = run_query(session,
                "MATCH (p:Person {name: $name}) RETURN id(p) AS id",
                {"name": new_name})
            if existing:
                _merge_person_nodes(session, p["id"], existing[0]["id"], p["name"], new_name)
            else:
                run_query(session,
                    "MATCH (p) WHERE id(p) = $id SET p.name = $name",
                    {"id": p["id"], "name": new_name})
                print(f"    Renamed: '{p['name']}' -> '{new_name}'")
            merged += 1
        elif len(graph_candidates) > 1:
            # Try co-occurrence disambiguation
            best = _disambiguate_by_cooccurrence(session, p["id"], graph_candidates)
            if best:
                _merge_person_nodes(session, p["id"], best["id"], p["name"], best["name"])
                merged += 1
            else:
                skipped_ambiguous += 1
        elif len(emp_candidates) > 1:
            # Multiple employee matches, no graph full-name - try co-occurrence too
            # First check if any of those employee names exist as Person nodes
            found = []
            for en in emp_candidates:
                existing = run_query(session,
                    "MATCH (p:Person {name: $name}) RETURN id(p) AS id, p.name AS name",
                    {"name": en})
                found.extend(existing)
            if len(found) == 1:
                _merge_person_nodes(session, p["id"], found[0]["id"], p["name"], found[0]["name"])
                merged += 1
            elif len(found) > 1:
                best = _disambiguate_by_cooccurrence(session, p["id"], found)
                if best:
                    _merge_person_nodes(session, p["id"], best["id"], p["name"], best["name"])
                    merged += 1
                else:
                    skipped_ambiguous += 1
            else:
                skipped_ambiguous += 1
        else:
            skipped_no_match += 1

    print(f"\n  Merged/renamed: {merged}")
    print(f"  Deleted (junk/email): {deleted}")
    print(f"  Skipped (ambiguous): {skipped_ambiguous}")
    print(f"  Skipped (no match): {skipped_no_match}")
    return merged + deleted


def _disambiguate_by_cooccurrence(session, source_id, candidates):
    """
    Given a first-name-only Person node and multiple full-name candidates,
    find which candidate shares the most meetings via ATTENDED relationships.
    Returns the best candidate or None if no clear winner.
    """
    # Get meetings the source node attended
    source_meetings = run_query(session, """
        MATCH (p)-[:ATTENDED]->(m:Meeting)
        WHERE id(p) = $id
        RETURN id(m) AS mid
    """, {"id": source_id})
    if not source_meetings:
        return None

    source_mids = {r["mid"] for r in source_meetings}
    best_candidate = None
    best_overlap = 0

    for cand in candidates:
        cand_meetings = run_query(session, """
            MATCH (p)-[:ATTENDED]->(m:Meeting)
            WHERE id(p) = $id
            RETURN id(m) AS mid
        """, {"id": cand["id"]})
        cand_mids = {r["mid"] for r in cand_meetings}
        overlap = len(source_mids & cand_mids)
        if overlap > best_overlap:
            best_overlap = overlap
            best_candidate = cand

    # Only merge if there's a clear winner (at least 1 shared meeting
    # and more than any other candidate)
    if best_overlap >= 1:
        return best_candidate
    return None


def _merge_person_nodes(session, source_id, target_id, source_name, target_name):
    """Merge source Person node into target using APOC mergeNodes."""
    try:
        run_query(session, """
            MATCH (source) WHERE id(source) = $source_id
            MATCH (target) WHERE id(target) = $target_id
            CALL apoc.refactor.mergeNodes([target, source], {
                properties: "discard",
                mergeRels: true
            }) YIELD node
            RETURN node
        """, {"source_id": source_id, "target_id": target_id})
        print(f"    Merged: '{source_name}' -> '{target_name}'")
    except Exception as e:
        print(f"    ERROR merging '{source_name}' -> '{target_name}': {e}")


# ═══════════════════════════════════════════════════════════════════════════
# 2. Clean Up Misclassified Clients
# ═══════════════════════════════════════════════════════════════════════════

def fix_clients(session):
    """Relabel or delete misclassified Client nodes."""
    print("\n" + "=" * 70)
    print(" STEP 2: Client Cleanup")
    print("=" * 70)

    clients_data = load_json(CLIENTS_FILE)
    real_client_names = set()
    for c in clients_data:
        name = c.get("client_name", "").strip()
        internal = c.get("internal_client_name", "").strip()
        code = c.get("client_code", "").strip()
        if name:
            real_client_names.add(name.lower())
        if internal:
            real_client_names.add(internal.lower())
        if code:
            real_client_names.add(code.lower())

    # Get all Client nodes
    client_nodes = run_query(session, "MATCH (c:Client) RETURN id(c) AS id, c.name AS name")
    print(f"  Total Client nodes: {len(client_nodes)}")

    # Known non-client patterns
    person_pattern = re.compile(r'^[A-Z][a-z]+ [A-Z][a-z]+$')  # "First Last"
    single_first_name = re.compile(r'^[A-Z][a-z]+$')  # "Catherine", "Sandra"
    generic_terms = {
        "client", "the client", "agency", "agency team", "agency client",
        "client leadership", "client team", "potential client", "unnamed client",
        "ppc client", "level",
    }
    # Location names that shouldn't be clients
    location_names = {
        "houston", "austin", "chicago", "california", "la", "los angeles",
        "new york", "new jersey", "maryland", "orange county", "san diego bsn",
        "riverside", "riverside lvn", "atl", "kop", "washington tax",
    }
    # Platform/tool names (keep as Organization but not Client)
    platform_names = {
        "meta", "linkedin", "indeed", "ziprecruiter", "google", "tiktok",
        "youtube", "instagram", "instacart", "ibm watson", "pardot", "sfmc",
        "vwo", "lead squared", "groundtruth", "ground truth", "dms",
        "barstool", "blackrock", "ishares", "comcast",
    }
    # Acronym-like short entries that don't match real clients
    tool_acronyms = {
        "mcs scorecard", "nss", "stv",
    }

    relabeled_org = 0
    deleted = 0
    kept = 0

    for node in client_nodes:
        name = (node["name"] or "").strip()
        name_lower = name.lower()

        # Check if it's a real client (fuzzy: check if name is substring of any real client or vice versa)
        is_real = False
        for rc in real_client_names:
            if name_lower == rc or name_lower in rc or rc in name_lower:
                is_real = True
                break

        if is_real:
            kept += 1
            continue

        # Check if it's a generic term
        if name_lower in generic_terms:
            _delete_entity_node(session, node["id"], name, "generic term")
            deleted += 1
            continue

        # Check if it's a location
        if name_lower in location_names:
            _relabel_to_organization(session, node["id"], name, "location")
            relabeled_org += 1
            continue

        # Check if it's a platform/tool
        if name_lower in platform_names:
            _relabel_to_organization(session, node["id"], name, "platform")
            relabeled_org += 1
            continue

        # Check if it looks like a person name (First Last)
        if person_pattern.match(name):
            _relabel_to_person(session, node["id"], name)
            relabeled_org += 1
            continue

        # Check if it's a single first name (likely a person)
        if single_first_name.match(name) and len(name) >= 3:
            _relabel_to_person(session, node["id"], name)
            relabeled_org += 1
            continue

        # Check if it's an email-like string or username
        if "@" in name or ("." in name and " " not in name and len(name) > 5):
            _delete_entity_node(session, node["id"], name, "email/username")
            deleted += 1
            continue

        # Keep anything else - might be a legitimate company
        kept += 1

    print(f"\n  Kept as Client: {kept}")
    print(f"  Relabeled (Organization/Person): {relabeled_org}")
    print(f"  Deleted: {deleted}")
    return relabeled_org + deleted


def _relabel_to_organization(session, node_id, name, reason):
    """Remove Client label, add Organization label."""
    run_query(session, """
        MATCH (n) WHERE id(n) = $id
        REMOVE n:Client
        SET n:Organization
    """, {"id": node_id})
    print(f"    Relabeled '{name}' -> Organization ({reason})")


def _relabel_to_person(session, node_id, name):
    """Remove Client label, add Person label. Merge if Person already exists."""
    existing = run_query(session,
        "MATCH (p:Person {name: $name}) RETURN id(p) AS id",
        {"name": name})
    if existing:
        # Merge into existing Person
        try:
            run_query(session, """
                MATCH (source) WHERE id(source) = $source_id
                MATCH (target) WHERE id(target) = $target_id
                CALL apoc.refactor.mergeNodes([target, source], {
                    properties: "discard",
                    mergeRels: true
                }) YIELD node
                RETURN node
            """, {"source_id": node_id, "target_id": existing[0]["id"]})
            print(f"    Merged Client '{name}' into existing Person")
        except Exception as e:
            print(f"    ERROR merging Client '{name}': {e}")
    else:
        run_query(session, """
            MATCH (n) WHERE id(n) = $id
            REMOVE n:Client
            SET n:Person
        """, {"id": node_id})
        print(f"    Relabeled '{name}' -> Person")


def _delete_entity_node(session, node_id, name, reason):
    """Delete an entity node and its relationships."""
    run_query(session, """
        MATCH (n) WHERE id(n) = $id
        DETACH DELETE n
    """, {"id": node_id})
    print(f"    Deleted '{name}' ({reason})")


# ═══════════════════════════════════════════════════════════════════════════
# 3. Clean Up Phantom Meetings
# ═══════════════════════════════════════════════════════════════════════════

def fix_phantom_meetings(session):
    """
    All 575 Meeting nodes are LLM-extracted __Entity__ nodes.
    Real meetings are Document nodes (49 of them).

    Strategy:
    - Meeting entities that match a Document topic exactly: keep, link to Document
    - Meeting entities with many relationships (ATTENDED, HOSTED, ABOUT): keep (useful)
    - Meeting entities with few/no meaningful relationships: relabel to Topic or delete
    - Generic meeting names: delete
    """
    print("\n" + "=" * 70)
    print(" STEP 3: Phantom Meeting Cleanup")
    print("=" * 70)

    # Get Document topics
    docs = run_query(session, "MATCH (d:Document) RETURN d.topic AS topic, id(d) AS id")
    doc_topics = {d["topic"].strip().lower(): d for d in docs if d["topic"]}

    # Get all Meeting entity nodes with relationship counts
    meetings = run_query(session, """
        MATCH (m:Meeting)
        OPTIONAL MATCH (p:Person)-[:ATTENDED]->(m)
        WITH m, count(DISTINCT p) AS attendees
        OPTIONAL MATCH (m)-[r]-()
        WHERE NOT type(r) = 'FROM_CHUNK'
        WITH m, attendees, count(r) AS meaningful_rels
        RETURN id(m) AS id, m.name AS name, attendees, meaningful_rels
        ORDER BY meaningful_rels DESC
    """)
    print(f"  Total Meeting entity nodes: {len(meetings)}")

    # Generic meeting names to delete outright
    generic_meeting_names = {
        "team meeting", "meeting", "status meeting", "weekly meeting",
        "performance review", "january performance review",
        "january marketing review", "january revenue performance review",
        "recording summary", "internal team meeting", "all-agency meeting",
        "project kickoff", "introductory meeting", "monthly reporting",
        "budget meeting", "follow-up email", "meeting offer",
        "meeting tomorrow", "3 pm meeting", "2026 meeting",
        "monthly recap meeting", "project updates meeting",
    }

    kept = 0
    relabeled = 0
    deleted = 0
    linked_to_doc = 0

    for m in meetings:
        name = (m["name"] or "").strip()
        name_lower = name.lower()

        # If matches a Document topic, keep it (these are real meetings)
        if name_lower in doc_topics:
            kept += 1
            linked_to_doc += 1
            continue

        # If it has 2+ attendees, keep it (useful graph structure)
        if m["attendees"] >= 2:
            kept += 1
            continue

        # Generic names -> delete
        if name_lower in generic_meeting_names:
            _delete_entity_node(session, m["id"], name, "generic meeting")
            deleted += 1
            continue

        # Meetings with 0 attendees -> relabel as Topic (these are purely
        # descriptive text extracted by the LLM, not real meetings)
        if m["attendees"] == 0:
            run_query(session, """
                MATCH (n) WHERE id(n) = $id
                REMOVE n:Meeting
                SET n:Topic
            """, {"id": m["id"]})
            relabeled += 1
            continue

        # Meetings with exactly 1 attendee -> relabel as Topic
        # (1 attendee likely means the LLM just linked a person mentioned
        # in the transcript to a meeting description it extracted)
        if m["attendees"] == 1:
            run_query(session, """
                MATCH (n) WHERE id(n) = $id
                REMOVE n:Meeting
                SET n:Topic
            """, {"id": m["id"]})
            relabeled += 1
            continue

        # Keep the rest
        kept += 1

    print(f"\n  Kept as Meeting: {kept} (of which {linked_to_doc} match Document topics)")
    print(f"  Relabeled to Topic: {relabeled}")
    print(f"  Deleted: {deleted}")
    return relabeled + deleted


# ═══════════════════════════════════════════════════════════════════════════
# 4. General Dedup (Levenshtein distance <= 2)
# ═══════════════════════════════════════════════════════════════════════════

def levenshtein(s1, s2):
    """Compute Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[len(s2)]


def dedup_entities(session):
    """Find and merge entities with very similar names (Levenshtein <= 2) within same label."""
    print("\n" + "=" * 70)
    print(" STEP 4: General Deduplication")
    print("=" * 70)

    # Labels to dedup (skip structural labels)
    skip_labels = {"__KGBuilder__", "__Entity__", "Chunk", "Document"}

    labels_result = run_query(session, """
        MATCH (n)
        WITH labels(n) AS lbls
        UNWIND lbls AS label
        WITH DISTINCT label
        WHERE NOT label IN $skip
        RETURN label
    """, {"skip": list(skip_labels)})

    total_merged = 0

    for label_row in labels_result:
        label = label_row["label"]
        nodes = run_query(session, f"""
            MATCH (n:`{label}`)
            WHERE n.name IS NOT NULL
            RETURN id(n) AS id, n.name AS name
            ORDER BY n.name
        """)

        if len(nodes) < 2:
            continue

        # Find pairs with Levenshtein distance <= 2
        # Sort by name length descending so we keep the longer (more specific) name
        nodes.sort(key=lambda x: -len(x["name"]))
        merged_ids = set()
        pairs = []

        for i in range(len(nodes)):
            if nodes[i]["id"] in merged_ids:
                continue
            for j in range(i + 1, len(nodes)):
                if nodes[j]["id"] in merged_ids:
                    continue
                n1, n2 = nodes[i]["name"], nodes[j]["name"]
                # Skip short names -- too many false positives for short strings
                min_len = min(len(n1), len(n2))
                if min_len < 6:
                    continue
                # Only allow distance 1 for names < 10 chars, distance 2 for longer
                max_dist = 1 if min_len < 10 else 2
                dist = levenshtein(n1.lower(), n2.lower())
                if 0 < dist <= max_dist:
                    # Additional check: names must share the same first word
                    # (prevents merging unrelated short names)
                    w1 = n1.lower().split()
                    w2 = n2.lower().split()
                    if len(w1) > 0 and len(w2) > 0 and w1[0] == w2[0]:
                        pairs.append((nodes[i], nodes[j], dist))
                    elif len(w1) == 1 and len(w2) == 1:
                        # Single-word names: only merge at distance 1
                        if dist <= 1:
                            pairs.append((nodes[i], nodes[j], dist))

        for target, source, dist in pairs:
            if source["id"] in merged_ids or target["id"] in merged_ids:
                continue
            try:
                run_query(session, """
                    MATCH (source) WHERE id(source) = $source_id
                    MATCH (target) WHERE id(target) = $target_id
                    CALL apoc.refactor.mergeNodes([target, source], {
                        properties: "discard",
                        mergeRels: true
                    }) YIELD node
                    RETURN node
                """, {"source_id": source["id"], "target_id": target["id"]})
                merged_ids.add(source["id"])
                total_merged += 1
                print(f"    [{label}] Merged: '{source['name']}' -> '{target['name']}' (dist={dist})")
            except Exception as e:
                print(f"    [{label}] ERROR merging '{source['name']}' -> '{target['name']}': {e}")

    print(f"\n  Total dedup merges: {total_merged}")
    return total_merged


# ═══════════════════════════════════════════════════════════════════════════
# 5. Report
# ═══════════════════════════════════════════════════════════════════════════

def print_report(before_counts, after_counts, stats):
    """Print before/after comparison."""
    print("\n" + "=" * 70)
    print(" FINAL REPORT")
    print("=" * 70)

    print("\n  Node Counts (Before -> After):")
    all_labels = sorted(set(list(before_counts.keys()) + list(after_counts.keys())))
    for label in all_labels:
        b = before_counts.get(label, 0)
        a = after_counts.get(label, 0)
        diff = a - b
        marker = f" ({diff:+d})" if diff != 0 else ""
        print(f"    {label:30s} {b:>6d} -> {a:>6d}{marker}")

    print(f"\n  Operations performed:")
    print(f"    Person merges/renames:       {stats.get('person_fixes', 0)}")
    print(f"    Client relabels/deletes:     {stats.get('client_fixes', 0)}")
    print(f"    Meeting relabels/deletes:    {stats.get('meeting_fixes', 0)}")
    print(f"    Dedup merges:                {stats.get('dedup_merges', 0)}")
    total = sum(stats.values())
    print(f"    TOTAL operations:            {total}")

    # Check remaining potential issues
    print("\n  Remaining potential issues:")
    remaining_first_only = run_query(session_ref[0], """
        MATCH (p:Person) WHERE NOT p.name CONTAINS ' '
        RETURN count(p) AS cnt
    """)
    cnt = remaining_first_only[0]["cnt"] if remaining_first_only else 0
    print(f"    First-name-only Person nodes: {cnt}")


# Global ref for session in report
session_ref = [None]


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    driver = neo4j.GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

    with driver.session() as session:
        session_ref[0] = session

        # ── Backup counts ───────────────────────────────────────────────
        print("Taking before-snapshot of node counts...")
        before_counts = get_node_counts(session)
        print("  Before counts:")
        for label, cnt in sorted(before_counts.items(), key=lambda x: -x[1]):
            print(f"    {label:30s} {cnt:>6d}")

        stats = {}

        # ── Step 1: Person names ────────────────────────────────────────
        stats["person_fixes"] = fix_person_names(session)

        # ── Step 2: Client cleanup ──────────────────────────────────────
        stats["client_fixes"] = fix_clients(session)

        # ── Step 3: Phantom meetings ────────────────────────────────────
        stats["meeting_fixes"] = fix_phantom_meetings(session)

        # ── Step 4: General dedup ───────────────────────────────────────
        stats["dedup_merges"] = dedup_entities(session)

        # ── Step 5: Report ──────────────────────────────────────────────
        after_counts = get_node_counts(session)
        print_report(before_counts, after_counts, stats)

    driver.close()
    print("\nDone.")


if __name__ == "__main__":
    main()

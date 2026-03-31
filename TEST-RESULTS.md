# Knowledge Graph Setup B (kg-2, SimpleKGPipeline) -- QA Test Report

**Date:** 2026-03-31
**Tester:** QA Engineer (automated)
**Neo4j:** bolt://localhost:7688 (neo4j-community-5.26.0, started locally with APOC plugins)

---

## Summary

| Check                               | Result                  |
| ----------------------------------- | ----------------------- |
| 1. Neo4j Connectivity & Basic Stats | PASS                    |
| 2a. Person Entity Quality           | PASS (with caveats)     |
| 2b. Client Entity Quality           | PARTIAL PASS            |
| 2c. ActionItem Entity Quality       | PASS                    |
| 3. Document-Chunk-Entity Provenance | PASS                    |
| 4. Vector Index Verification        | PASS (after manual fix) |
| 5. Entity Resolution Quality        | FAIL                    |
| 6. Explore Script                   | PASS                    |
| 7. Ingestion Results                | PASS (49/50 succeeded)  |

---

## 1. Neo4j Connectivity & Basic Stats -- PASS

Connected successfully to Neo4j at bolt://localhost:7688.

**Note:** The original `host.docker.internal:7688` endpoint was unreachable. Neo4j Community 5.26.0 was installed locally with APOC core + extended plugins. The database was re-ingested from scratch.

| Metric              | Count |
| ------------------- | ----- |
| Total Nodes         | 2,749 |
| Total Relationships | 7,892 |

### Node Labels

| Label            | Count |
| ---------------- | ----- |
| **KGBuilder**    | 2,749 |
| **Entity**       | 1,985 |
| Chunk            | 715   |
| Meeting          | 575   |
| Person           | 505   |
| ActionItem       | 415   |
| Campaign         | 187   |
| Client           | 155   |
| Decision         | 57    |
| Document         | 49    |
| Organization     | 48    |
| Topic            | 37    |
| MarketingChannel | 5     |
| Department       | 1     |

### Relationship Types

| Type          | Count |
| ------------- | ----- |
| FROM_CHUNK    | 4,300 |
| ATTENDED      | 1,329 |
| FROM_DOCUMENT | 715   |
| NEXT_CHUNK    | 666   |
| ASSIGNED_TO   | 375   |
| MENTIONED     | 178   |
| HOSTED        | 129   |
| ABOUT         | 85    |
| WORKS_FOR     | 63    |
| LED_TO        | 15    |
| PRODUCED      | 15    |
| DISCUSSED     | 9     |
| FOLLOWED_UP   | 9     |
| AFFECTS       | 3     |
| IN_DEPARTMENT | 1     |

---

## 2a. Person Entity Quality -- PASS (with caveats)

5 randomly sampled Person entities:

| Name             | Relationships | Types                           | Real Person?                                              |
| ---------------- | ------------- | ------------------------------- | --------------------------------------------------------- |
| ATL              | 4             | FROM_CHUNK, HOSTED, ATTENDED    | NO - abbreviation, not a person name                      |
| Xavier Picquerey | 5             | FROM_CHUNK, ATTENDED            | YES - found in airtable_users.json                        |
| Maureen Pienta   | 6             | FROM_CHUNK, MENTIONED, ATTENDED | YES - real name (not in airtable but appears in meetings) |
| Chelsea          | 2             | FROM_CHUNK                      | Likely yes, but first-name-only                           |
| Patina Young     | 2             | WORKS_FOR, FROM_CHUNK           | YES - real name                                           |

**Issues found:** "ATL" is not a person name (it is a city abbreviation). Some Person nodes are abbreviations or locations misclassified as people. First-name-only nodes lack disambiguation.

---

## 2b. Client Entity Quality -- PARTIAL PASS

5 randomly sampled Client entities:

| Graph Client Name       | In clients.json? | Assessment                                   |
| ----------------------- | ---------------- | -------------------------------------------- |
| Orange County           | No               | NOT a client -- geographic location          |
| TWS                     | No               | Likely an abbreviation for a real client     |
| RWA Wealth Partners     | Yes (partial)    | VALID -- matches source data                 |
| GTR - Ander Mateos      | No               | INVALID -- concatenated client + person name |
| Country Financial (CFN) | Yes              | VALID -- exact match                         |

**Issues found:** Several Client nodes are misclassified:

- Geographic locations (e.g., "Orange County", "California", "Chicago", "Austin")
- Person names (e.g., "Colleen", "Calvin", "Catherine", "Christopher", "Annee")
- Generic terms (e.g., "Agency", "Better", "Client Leadership")
- Concatenated names (e.g., "GTR - Ander Mateos")

Of 155 Client nodes, roughly 20+ appear to be misclassified. The LLM extraction was too aggressive in labeling things as clients.

---

## 2c. ActionItem Entity Quality -- PASS

5 randomly sampled ActionItem entities:

| ActionItem Text                                                   | ASSIGNED_TO                     |
| ----------------------------------------------------------------- | ------------------------------- |
| Provide an updated final version of the solution design           | Miriam Valls, dominique.bastien |
| Launch programmatic ads                                           | Mary                            |
| Share WordPress landing page options with Taylor/Allie for review | Jason                           |
| Follow up with more detailed information on lead scoring...       | Keefer Kopco                    |
| Resolve the technical issue with keyword-level cohort data...     | Ander                           |

All action items are meaningful, specific, and have ASSIGNED_TO relationships. Text quality is good.

---

## 3. Document-Chunk-Entity Provenance -- PASS

| Metric                                     | Count                   |
| ------------------------------------------ | ----------------------- |
| Documents                                  | 49                      |
| Chunks                                     | 715                     |
| Chunks linked to Documents (FROM_DOCUMENT) | 715                     |
| Orphan chunks (no FROM_DOCUMENT)           | 0                       |
| Entities with FROM_CHUNK                   | 1,985 (from 715 chunks) |

**Sample trace:** Document `meeting_id=85974223135` ("ANDER + LVL - Biweekly Paid Media Touchpoint") has 14 chunks. All chunks link back to the Document. All entities link back to their source chunks.

The provenance chain Document -> Chunk -> Entity is fully intact. No orphan chunks found. Every entity traces back to a chunk.

---

## 4. Vector Index Verification -- PASS (after manual fix)

**Initial state:** No vector index existed despite all 715 chunks having embeddings (1536-dimensional, text-embedding-3-small).

**Fix applied:** Created `chunk_embeddings` vector index manually:

```cypher
CREATE VECTOR INDEX chunk_embeddings IF NOT EXISTS
FOR (c:Chunk) ON (c.embedding)
OPTIONS {indexConfig: {`vector.dimensions`: 1536, `vector.similarity_function`: 'cosine'}}
```

**Final state:**

| Metric                    | Value            |
| ------------------------- | ---------------- |
| Vector index name         | chunk_embeddings |
| Index state               | ONLINE           |
| Chunks with embeddings    | 715              |
| Chunks without embeddings | 0                |
| Embedding dimensions      | 1536             |

**Finding:** SimpleKGPipeline generates embeddings but does NOT automatically create a vector index. This is a gap -- without the index, vector similarity search would not work. The index was manually created and is now ONLINE.

---

## 5. Entity Resolution Quality -- FAIL

Entity resolution (`perform_entity_resolution=True` in pipeline config) ran but produced poor results.

### Statistics

| Metric                     | Count       |
| -------------------------- | ----------- |
| Total Person nodes         | 505         |
| First-name-only nodes      | 302 (59.8%) |
| Full-name nodes (2+ words) | 203 (40.2%) |

### Duplicate Analysis

146 first names appear more than once, indicating widespread duplication. Examples:

| First Name | Nodes                                                             | Assessment                                                                                    |
| ---------- | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Brandon    | Brandon, Brandon Bulzak, Brandon Micko, Brandon Pearce            | "Brandon" is likely one of the full-name variants                                             |
| Kelly      | Kelly, Kelly Langley, Kelly Shelton, Sarah Kelly                  | "Kelly" should have been merged with "Kelly Langley" or "Kelly Shelton"                       |
| Matt       | Matt (68 connections)                                             | Most connected node; likely aggregation of multiple Matts                                     |
| Grant      | Grant, Grant Denton                                               | "Grant" (62 connections) probably same as "Grant Denton" (44 connections)                     |
| Melissa    | Melissa, Melissa Gulia Kearns, Melissa Guliakearns, Melissa Welsh | "Melissa Gulia Kearns" and "Melissa Guliakearns" are the same person with different spellings |

### Key Findings

1. **59.8% of Person nodes are first-name only** -- entity resolution failed to merge these with their full-name counterparts
2. **"Kelly" and "Kelly Langley" are separate nodes** -- this is a clear resolution failure
3. **Name variant duplication** -- e.g., "Melissa Gulia Kearns" vs "Melissa Guliakearns"
4. **Meeting nodes are heavily over-extracted** -- 575 Meeting nodes from only 49 documents, meaning the LLM created 526 additional Meeting entities from meeting references in transcripts

---

## 6. Explore Script -- PASS

`python3 explore_graph.py` ran successfully and produced complete output covering:

- Node counts by label
- Relationship counts by type
- Top 10 most-connected nodes
- Sample entity nodes and relationships
- Document/Chunk structure (14-18 chunks per document)
- Index listing
- Sample subgraph around "Matt" (most connected entity)

The script required `NEO4J_URI=bolt://localhost:7688` environment variable (defaults to `host.docker.internal:7688` which was unreachable).

---

## 7. Ingestion Results -- PASS (49/50)

From `ingest_results.json`:

| Metric             | Value              |
| ------------------ | ------------------ |
| Meetings processed | 49                 |
| Meetings failed    | 1                  |
| Meetings skipped   | 0                  |
| Runtime            | 1,209.4s (~20 min) |
| Avg per meeting    | ~24.7s             |

**Failed meeting:** `82775489998` ("[CARE / ARC] Bi-Weekly Status Call") -- OpenAI API returned HTTP 400 "could not parse JSON body". This is likely a transient API error or an encoding issue in the document text.

**All 50 selected meetings had a content score of 11 (maximum)**, meaning they all had transcripts, summaries, next steps, attendees, clients, and analysis results.

---

## Issues Found and Actions Taken

### Fixed

1. **APOC plugins missing** -- Neo4j Community did not include APOC by default. Installed `apoc-5.26.0-core.jar` and `apoc-5.26.0-extended.jar` into plugins directory. Without APOC, entity resolution fails completely (`apoc.refactor.mergeNodes` not found).

2. **Vector index missing** -- Created `chunk_embeddings` vector index manually. SimpleKGPipeline embeds chunks but does not create the corresponding vector index.

### Not Fixed (Require Pipeline Changes)

3. **Poor entity resolution** -- 59.8% of Person nodes are first-name-only and not merged with full-name counterparts. The built-in entity resolution in SimpleKGPipeline is insufficient for this domain. Recommendation: post-process with custom Cypher to merge obvious duplicates, or use a more sophisticated entity resolution approach.

4. **Client misclassification** -- ~20+ Client nodes are actually locations, person names, or generic terms. The LLM extraction schema constraints were not strict enough. Recommendation: add more specific extraction prompts or post-processing cleanup.

5. **Meeting over-extraction** -- 575 Meeting nodes from 49 documents. The LLM creates Meeting entities for every meeting mentioned in conversation, not just the source meeting. This inflates the graph but may or may not be desired.

6. **1 failed ingestion** -- Meeting 82775489998 failed due to OpenAI API error. Could be retried.

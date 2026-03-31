#!/usr/bin/env python3
"""
Diagnostic script to explore the Knowledge Graph in Neo4j.

Prints:
- Node count by label
- Relationship count by type
- Top 10 most-connected nodes
- Sample subgraph around a specific meeting
- Index information
"""

import os
import json
from dotenv import load_dotenv
import neo4j

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://host.docker.internal:7688")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "kg-eval-password")


def run_query(session, query, params=None):
    """Run a Cypher query and return results as list of dicts."""
    result = session.run(query, params or {})
    return [dict(r) for r in result]


def explore_graph():
    driver = neo4j.GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

    with driver.session() as session:
        print("=" * 70)
        print(" KNOWLEDGE GRAPH EXPLORATION REPORT")
        print("=" * 70)

        # 1. Node count by label
        print("\n--- Node Count by Label ---")
        results = run_query(session, """
            MATCH (n)
            WITH labels(n) AS lbls
            UNWIND lbls AS label
            WITH label, count(*) AS cnt
            RETURN label, cnt
            ORDER BY cnt DESC
        """)
        total_nodes = 0
        for r in results:
            print(f"  {r['label']:30s} {r['cnt']:>8,d}")
            total_nodes += r['cnt']
        print(f"  {'TOTAL':30s} {total_nodes:>8,d}")

        # 2. Relationship count by type
        print("\n--- Relationship Count by Type ---")
        results = run_query(session, """
            MATCH ()-[r]->()
            RETURN type(r) AS rel_type, count(*) AS cnt
            ORDER BY cnt DESC
        """)
        total_rels = 0
        for r in results:
            print(f"  {r['rel_type']:30s} {r['cnt']:>8,d}")
            total_rels += r['cnt']
        print(f"  {'TOTAL':30s} {total_rels:>8,d}")

        # 3. Top 10 most-connected nodes
        print("\n--- Top 10 Most-Connected Nodes ---")
        results = run_query(session, """
            MATCH (n)
            WHERE NOT n:Chunk AND NOT n:Document
            WITH n, size([(n)-[]-() | 1]) AS degree
            RETURN labels(n) AS labels, n.name AS name, degree
            ORDER BY degree DESC
            LIMIT 10
        """)
        for r in results:
            labels = ", ".join(r["labels"]) if r["labels"] else "?"
            name = r["name"] or "(no name)"
            print(f"  [{labels}] {name:40s} connections: {r['degree']}")

        # 4. Sample entity nodes (non-structural)
        print("\n--- Sample Entity Nodes (first 20) ---")
        results = run_query(session, """
            MATCH (n)
            WHERE NOT n:Chunk AND NOT n:Document AND NOT n:__KGBuilder__
            RETURN labels(n) AS labels, n.name AS name,
                   properties(n) AS props
            LIMIT 20
        """)
        for r in results:
            labels = ", ".join(r["labels"]) if r["labels"] else "?"
            name = r.get("name") or "(no name)"
            props = {k: str(v)[:60] for k, v in (r.get("props") or {}).items()
                     if k not in ("name", "embedding")}
            print(f"  [{labels}] {name}")
            if props:
                print(f"    props: {json.dumps(props, default=str)[:200]}")

        # 5. Sample relationships
        print("\n--- Sample Relationships (first 20) ---")
        results = run_query(session, """
            MATCH (a)-[r]->(b)
            WHERE NOT a:Chunk AND NOT b:Chunk
              AND NOT a:Document AND NOT b:Document
            RETURN labels(a) AS a_labels, a.name AS a_name,
                   type(r) AS rel_type,
                   labels(b) AS b_labels, b.name AS b_name
            LIMIT 20
        """)
        for r in results:
            a_lbl = ", ".join(r["a_labels"]) if r["a_labels"] else "?"
            b_lbl = ", ".join(r["b_labels"]) if r["b_labels"] else "?"
            print(f"  [{a_lbl}] {r['a_name'] or '?'} --[{r['rel_type']}]--> [{b_lbl}] {r['b_name'] or '?'}")

        # 6. Document/Chunk structure
        print("\n--- Document/Chunk Structure ---")
        results = run_query(session, """
            MATCH (d:Document)
            OPTIONAL MATCH (d)<-[:FROM_DOCUMENT]-(c:Chunk)
            WITH d, count(c) AS chunk_count
            RETURN d.path AS path,
                   d.meeting_id AS meeting_id,
                   d.topic AS topic,
                   chunk_count
            ORDER BY chunk_count DESC
            LIMIT 10
        """)
        for r in results:
            topic = r.get("topic") or r.get("path") or r.get("meeting_id") or "?"
            print(f"  {str(topic)[:50]:50s} chunks: {r['chunk_count']}")

        # 7. Indexes
        print("\n--- Indexes ---")
        try:
            results = run_query(session, "SHOW INDEXES")
            for r in results:
                idx_name = r.get("name", "?")
                idx_type = r.get("type", "?")
                labels = r.get("labelsOrTypes", [])
                props = r.get("properties", [])
                state = r.get("state", "?")
                print(f"  {idx_name:30s} type={idx_type:15s} on={labels} props={props} state={state}")
        except Exception as e:
            print(f"  Could not list indexes: {e}")

        # 8. Subgraph sample: pick a well-connected entity
        print("\n--- Sample Subgraph (around most-connected entity) ---")
        top_node = run_query(session, """
            MATCH (n)
            WHERE NOT n:Chunk AND NOT n:Document AND n.name IS NOT NULL
            WITH n, size([(n)-[]-() | 1]) AS degree
            ORDER BY degree DESC
            LIMIT 1
            MATCH (n)-[r]-(m)
            WHERE NOT m:Chunk AND NOT m:Document
            RETURN labels(n)[0] AS center_label, n.name AS center_name,
                   type(r) AS rel_type,
                   labels(m)[0] AS neighbor_label, m.name AS neighbor_name
            LIMIT 15
        """)
        if top_node:
            center = f"[{top_node[0].get('center_label', '?')}] {top_node[0].get('center_name', '?')}"
            print(f"  Center: {center}")
            for r in top_node:
                print(f"    --[{r['rel_type']}]--> [{r.get('neighbor_label', '?')}] {r.get('neighbor_name', '?')}")
        else:
            print("  No entity nodes found.")

    driver.close()
    print("\n" + "=" * 70)
    print(" Exploration complete.")
    print("=" * 70)


if __name__ == "__main__":
    explore_graph()

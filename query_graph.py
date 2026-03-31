#!/usr/bin/env python3
"""
CLI query interface for the Knowledge Graph.

Supports:
- Vector search (semantic similarity)
- Text2Cypher (natural language to Cypher)
- Direct Cypher queries
"""

import asyncio
import os
import sys
import json
from dotenv import load_dotenv

import neo4j
from neo4j_graphrag.llm import OpenAILLM
from neo4j_graphrag.embeddings import OpenAIEmbeddings

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://host.docker.internal:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "kg-eval-password")


def get_driver():
    return neo4j.GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))


def vector_search(driver, query_text, top_k=5):
    """Search for similar meeting summaries using vector embeddings."""
    embedder = OpenAIEmbeddings(model="text-embedding-3-small")

    try:
        from neo4j_graphrag.retrievers import VectorRetriever
        retriever = VectorRetriever(
            driver=driver,
            index_name="summary_embeddings",
            embedder=embedder,
            return_properties=["text"],
        )
        results = retriever.search(query_text=query_text, top_k=top_k)
        return results
    except Exception as e:
        print(f"VectorRetriever failed: {e}")
        return None


NEO4J_SCHEMA = """
Node labels and properties:
  Meeting (topic, start_time, end_time, uuid, meeting_id, sentiment_score, meeting_type, is_recurring, host_email)
  Person (name, email, department, role_type, seniority_level, first_name, last_name, status)
  Client (client_name, client_code, internal_client_name, airtable_id, hubspot_id)
  ActionItem (description, action_id, source_meeting_uuid)
  Topic (label, summary, topic_id)
  Summary (text, summary_id, source)
  Analysis (analysis_type, output_markdown, analysis_datetime, status)
  SlackChannel (channel_name, slack_channel_id)
  Domain (name)

Relationships:
  (Meeting)-[:ABOUT]->(Client)
  (Person)-[:ATTENDED]->(Meeting)
  (Person)-[:INVITED_TO]->(Meeting)
  (Meeting)-[:PRODUCED]->(ActionItem)
  (Meeting)-[:DISCUSSED]->(Topic)
  (Meeting)-[:HAS_SUMMARY]->(Summary)
  (Meeting)-[:HAS_ANALYSIS]->(Analysis)
  (SlackChannel)-[:HOSTED]->(Meeting)
  (Person)-[:WORKS_FOR]->(Client)
  (Person)-[:HAS_ROLE]->(Client)
  (Client)-[:HAS_DOMAIN]->(Domain)
  (Client)-[:HAS_CHANNEL]->(SlackChannel)
  (Person)-[:MEMBER_OF]->(SlackChannel)

Important: Client name is stored in client_name (not name). ActionItem text is in description (not name). Topic title is in label (not name).
"""


def text2cypher_search(driver, question):
    """Convert natural language to Cypher and execute."""
    llm = OpenAILLM(
        model_name="gpt-4o-mini",
        model_params={"temperature": 0},
    )

    try:
        from neo4j_graphrag.retrievers import Text2CypherRetriever
        retriever = Text2CypherRetriever(
            driver=driver,
            llm=llm,
            neo4j_schema=NEO4J_SCHEMA,
        )
        result = retriever.search(query_text=question)
        return result
    except Exception as e:
        print(f"Text2Cypher failed: {e}")
        return None


def cypher_query(driver, query, params=None):
    """Execute a raw Cypher query."""
    with driver.session() as session:
        result = session.run(query, params or {})
        records = [dict(r) for r in result]
        return records


def hybrid_search(driver, question, top_k=5):
    """Hybrid search combining vector and graph traversal."""
    embedder = OpenAIEmbeddings(model="text-embedding-3-small")
    llm = OpenAILLM(
        model_name="gpt-4o-mini",
        model_params={"temperature": 0},
    )

    try:
        from neo4j_graphrag.retrievers import VectorCypherRetriever
        # This retriever does vector search then follows graph relationships
        retriever = VectorCypherRetriever(
            driver=driver,
            index_name="summary_embeddings",
            embedder=embedder,
            retrieval_query="""
                WITH node, score
                OPTIONAL MATCH (m:Meeting)-[:HAS_SUMMARY]->(node)
                OPTIONAL MATCH (m)-[:ABOUT]->(c:Client)
                OPTIONAL MATCH (m)-[:DISCUSSED]->(t:Topic)
                RETURN node.text AS summary_text,
                       score,
                       m.topic AS meeting_topic,
                       c.client_name AS client,
                       collect(DISTINCT t.label)[..3] AS topics
                ORDER BY score DESC
                LIMIT 10
            """,
        )
        results = retriever.search(query_text=question, top_k=top_k)
        return results
    except Exception as e:
        print(f"Hybrid search failed: {e}")
        return None


def graphrag_search(driver, question):
    """Full GraphRAG: retrieve context and generate answer."""
    embedder = OpenAIEmbeddings(model="text-embedding-3-small")
    llm = OpenAILLM(
        model_name="gpt-4o-mini",
        model_params={"temperature": 0.1, "max_tokens": 1000},
    )

    try:
        from neo4j_graphrag.generation import GraphRAG
        from neo4j_graphrag.retrievers import VectorRetriever

        retriever = VectorRetriever(
            driver=driver,
            index_name="summary_embeddings",
            embedder=embedder,
            return_properties=["text"],
        )

        rag = GraphRAG(
            retriever=retriever,
            llm=llm,
        )

        result = rag.search(query_text=question)
        return result
    except Exception as e:
        print(f"GraphRAG search failed: {e}")
        return None


def print_results(results, label="Results"):
    """Pretty-print search results."""
    print(f"\n{'='*60}")
    print(f" {label}")
    print(f"{'='*60}")
    if results is None:
        print("  No results (search failed)")
        return
    if hasattr(results, "items"):
        for item in results.items:
            print(f"\n  Content: {str(item.content)[:200]}")
            if hasattr(item, "metadata") and item.metadata:
                print(f"  Metadata: {json.dumps(item.metadata, indent=4, default=str)[:300]}")
    elif hasattr(results, "answer"):
        print(f"\n  Answer: {results.answer}")
        if hasattr(results, "retriever_result"):
            print(f"\n  Context used: {str(results.retriever_result)[:500]}")
    elif isinstance(results, list):
        for r in results[:10]:
            print(f"  {json.dumps(r, indent=2, default=str)[:300]}")
    else:
        print(f"  {str(results)[:1000]}")


def interactive_mode(driver):
    """Interactive query mode."""
    print("\nKnowledge Graph Query Interface")
    print("Commands:")
    print("  v <query>  - Vector search")
    print("  t <query>  - Text2Cypher")
    print("  h <query>  - Hybrid search (vector + graph)")
    print("  g <query>  - GraphRAG (answer generation)")
    print("  c <cypher> - Raw Cypher query")
    print("  q          - Quit")
    print()

    while True:
        try:
            user_input = input("kg> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue

        if user_input.lower() == "q":
            break

        parts = user_input.split(" ", 1)
        cmd = parts[0].lower()
        query = parts[1] if len(parts) > 1 else ""

        if cmd == "v" and query:
            results = vector_search(driver, query)
            print_results(results, "Vector Search")
        elif cmd == "t" and query:
            results = text2cypher_search(driver, query)
            print_results(results, "Text2Cypher")
        elif cmd == "h" and query:
            results = hybrid_search(driver, query)
            print_results(results, "Hybrid Search")
        elif cmd == "g" and query:
            results = graphrag_search(driver, query)
            print_results(results, "GraphRAG Answer")
        elif cmd == "c" and query:
            try:
                results = cypher_query(driver, query)
                print_results(results, "Cypher Query")
            except Exception as e:
                print(f"  Cypher error: {e}")
        else:
            print("  Unknown command. Use v/t/h/g/c <query> or q to quit.")


def run_sample_queries(driver):
    """Run a set of sample queries to demonstrate capabilities."""
    print("\n" + "="*60)
    print(" Running Sample Queries")
    print("="*60)

    # 1. Basic graph stats
    print("\n--- Graph Statistics ---")
    stats = cypher_query(driver, """
        MATCH (n)
        WITH labels(n) AS lbls, count(*) AS cnt
        UNWIND lbls AS label
        RETURN label, sum(cnt) AS count
        ORDER BY count DESC
    """)
    print_results(stats, "Node Counts")

    # 2. Vector search example
    print("\n--- Vector Search: 'campaign performance review' ---")
    results = vector_search(driver, "campaign performance review")
    print_results(results, "Vector Search Results")

    # 3. GraphRAG example
    print("\n--- GraphRAG: 'What action items were discussed in client meetings?' ---")
    results = graphrag_search(driver, "What action items were discussed in client meetings?")
    print_results(results, "GraphRAG Answer")


if __name__ == "__main__":
    driver = get_driver()

    if len(sys.argv) > 1 and sys.argv[1] == "--sample":
        run_sample_queries(driver)
    elif len(sys.argv) > 1 and sys.argv[1] == "--interactive":
        interactive_mode(driver)
    else:
        # Default: run sample queries then enter interactive mode
        run_sample_queries(driver)
        interactive_mode(driver)

    driver.close()

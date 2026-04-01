#!/bin/bash
# Run KG-2 GraphRAG query interface (vector search, Text2Cypher, GraphRAG)
cd "$(dirname "$0")"
[ ! -d venv ] && python3 -m venv venv && venv/bin/pip install -q "neo4j-graphrag[openai]" neo4j python-dotenv
venv/bin/python query_graph.py

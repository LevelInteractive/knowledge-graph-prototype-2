#!/bin/bash
# Run KG-2 exploration queries
cd "$(dirname "$0")"
[ ! -d venv ] && python3 -m venv venv && venv/bin/pip install -q "neo4j-graphrag[openai]" neo4j python-dotenv
venv/bin/python explore_graph.py


# KG-2: neo4j-graphrag-python SimpleKGPipeline

Knowledge graph built using Neo4j's official GraphRAG library with LLM-based entity/relationship extraction from meeting transcripts.

## What's Here

```
ingest_transcripts.py    # Main pipeline - loads meetings, extracts entities via LLM
query_graph.py           # CLI query interface (vector, GraphRAG, Text2Cypher, raw Cypher)
explore_graph.py         # Diagnostic script (node/rel counts, top nodes, samples)
ingest_results.json      # Results from the 50-meeting ingestion run
.env                     # (gitignored) Environment variables
EVAL-NOTES.md            # Detailed evaluation notes
```

## Graph Stats (50 meetings)

- **3,150 nodes**: 2,104 entities (528 Person, 453 ActionItem, 203 Campaign, 152 Client, 66 Decision, 55 Topic) + 976 chunks + 70 documents
- **10,158 relationships** across 14 types
- **976 chunks** with vector embeddings for semantic search
- **Cost**: ~$0.52 for 50 meetings (~$15 projected for all 1,477)

## Prerequisites

- Python 3.11+
- Neo4j instance running (tested with Neo4j 5.x) with APOC plugin
- Data export at `/workspace/kg_export/` (or update paths in scripts)
- OpenAI API key (for LLM extraction and embeddings)

## Setup from Clone

```bash
cd /workspace/kg-2

# 1. Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install "neo4j-graphrag[openai]" neo4j python-dotenv

# 3. Create .env
cat > .env << 'EOF'
NEO4J_URI=bolt://localhost:7691
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your-password
OPENAI_API_KEY=your-openai-key
ANTHROPIC_API_KEY=your-anthropic-key  # optional
GEMINI_API_KEY=your-gemini-key        # optional (no direct support yet)
EOF

# 4. Create required Neo4j indexes (the pipeline creates these automatically, but you can verify)
# Vector index for chunk embeddings is created by SimpleKGPipeline

# 5. Run ingestion (processes 50 meetings by default)
python3 ingest_transcripts.py

# 6. Explore the graph
python3 explore_graph.py

# 7. Query the graph
python3 query_graph.py
```

## How It Was Tested

1. **Ingestion**: Ran `ingest_transcripts.py` which processed 50 meetings (selected by content richness). Each meeting's metadata + transcript/summary text is fed through SimpleKGPipeline for LLM entity extraction. 100% success rate, ~19 minutes total.
2. **Vector search**: Tested semantic search over chunk embeddings - returns relevant chunks with cosine similarity scores of 0.74-0.76.
3. **GraphRAG**: End-to-end Q&A working - retrieves context chunks, generates natural language answers via LLM.
4. **Text2Cypher**: Correctly generates Cypher queries from natural language questions.
5. **Explore script**: Verified node/relationship counts, top connected nodes, sample subgraphs.

## How to Test

```bash
# After ingestion, verify graph:
python3 explore_graph.py

# Query examples:
python3 query_graph.py
# Then choose:
#   1 = Vector search ("meetings about campaign performance")
#   2 = GraphRAG Q&A ("What action items came from BTR meetings?")
#   3 = Text2Cypher ("Show all people who attended meetings with Kelly")
#   4 = Raw Cypher ("MATCH (p:Person)-[:ATTENDED]->(m) RETURN p.name, count(m) LIMIT 10")

# Quick verification:
python3 -c "
from neo4j import GraphDatabase
d = GraphDatabase.driver('bolt://localhost:7691', auth=('neo4j','your-password'))
with d.session() as s:
    r = s.run('MATCH (n) RETURN labels(n)[0] as label, count(*) as cnt ORDER BY cnt DESC')
    for rec in r: print(f'{rec[\"label\"]}: {rec[\"cnt\"]}')
d.close()
"
```

## Gitignored Files (need recreation)

- `venv/` - Python virtual environment (`python3 -m venv venv && pip install "neo4j-graphrag[openai]" neo4j python-dotenv`)
- `.env` - Create manually with Neo4j and API credentials (see setup instructions above)

## LLM Notes

- Used **OpenAI GPT-4o-mini** for extraction (neo4j-graphrag has no direct Gemini API key support; VertexAI requires GCP service account)
- Used **text-embedding-3-small** for chunk embeddings (1536 dimensions)
- Entity resolution runs automatically but is basic (doesn't merge "Kelly" with "Kelly Langley")

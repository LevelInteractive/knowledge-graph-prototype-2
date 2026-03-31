# Knowledge Graph Setup B: neo4j-graphrag-python SimpleKGPipeline

## Setup Summary

- **Library**: neo4j-graphrag v1.14.1 (pip package: `neo4j-graphrag[openai,google]`)
- **LLM**: OpenAI GPT-4o-mini (Gemini not directly supported by neo4j-graphrag; VertexAI integration exists but requires GCP service account, not API key)
- **Embeddings**: OpenAI text-embedding-3-small (1536 dimensions)
- **Pipeline**: SimpleKGPipeline with FixedSizeSplitter (2000 char chunks, 200 overlap)
- **Entity Resolution**: Built-in (perform_entity_resolution=True)

## LLM Integration Notes

The neo4j-graphrag library supports: OpenAILLM, AnthropicLLM, VertexAILLM, CohereLLM, MistralAILLM, OllamaLLM.
No direct Google GenAI / Gemini API key integration. VertexAI requires GCP project credentials (service account JSON), not a simple API key. Fell back to OpenAI GPT-4o-mini as the most straightforward option.

## Ingestion Results

- **Meetings processed**: 50 out of 1,477 total (selected by content richness score)
- **Failures**: 0 (100% success rate)
- **Skipped**: 0
- **Pipeline runtime**: 1,149 seconds (~19.2 minutes, ~23 seconds per meeting)
- **Average document size**: 25,627 chars per meeting

## Graph Statistics

### Node Counts

| Label                               | Count     |
| ----------------------------------- | --------- |
| **KGBuilder** (internal)            | 3,150     |
| **Entity** (all extracted entities) | 2,104     |
| Chunk                               | 976       |
| Meeting                             | 595       |
| Person                              | 528       |
| ActionItem                          | 453       |
| Campaign                            | 203       |
| Client                              | 152       |
| Document                            | 70        |
| Decision                            | 66        |
| Topic                               | 55        |
| Organization                        | 43        |
| MarketingChannel                    | 9         |
| **Total**                           | **8,404** |

### Relationship Counts

| Type          | Count      |
| ------------- | ---------- |
| FROM_CHUNK    | 5,861      |
| ATTENDED      | 1,421      |
| FROM_DOCUMENT | 976        |
| NEXT_CHUNK    | 907        |
| ASSIGNED_TO   | 409        |
| MENTIONED     | 219        |
| HOSTED        | 153        |
| ABOUT         | 94         |
| WORKS_FOR     | 58         |
| PRODUCED      | 17         |
| LED_TO        | 17         |
| DISCUSSED     | 14         |
| FOLLOWED_UP   | 9          |
| AFFECTS       | 3          |
| **Total**     | **10,158** |

## Entity Resolution

The built-in entity resolution ran automatically after ingestion. Results from the test run showed `number_of_nodes_to_resolve: 531, number_of_created_nodes: 531`. The resolution uses fuzzy matching on entity names within the same label type.

**Observed issues**: Some entities like "Kelly", "Grant", "Matt" appear as first-name-only nodes alongside full-name nodes like "Grant Denton" and "Alicia Levey". The built-in resolver does not merge these partial-name matches. A more sophisticated resolution pass could improve this.

## Quality Assessment

### What Worked Well

1. **Entity extraction quality**: The LLM correctly identified Persons, Clients, Campaigns, ActionItems, and Decisions from meeting transcripts and summaries
2. **Relationship extraction**: ATTENDED and HOSTED relationships are well-populated (1,421 + 153), connecting people to meetings effectively
3. **Action items**: 453 ActionItem nodes with 409 ASSIGNED_TO relationships -- good coverage of who needs to do what
4. **Client associations**: 94 ABOUT relationships linking meetings to clients
5. **Campaign tracking**: 203 Campaign nodes with 219 MENTIONED relationships
6. **Zero failures**: All 50 meetings processed without errors
7. **Vector search works**: Semantic search over chunks returns relevant results with good cosine similarity scores (0.74-0.76)
8. **GraphRAG answers**: End-to-end question answering works, combining vector retrieval with LLM generation

### What Could Be Improved

1. **First-name-only entities**: Many Person nodes are first-name-only ("Kelly", "Grant", "Matt") rather than full names. Could be improved with better prompt engineering or post-processing
2. **Entity resolution**: Built-in resolver doesn't merge "Kelly" with "Kelly Langley" or "Grant" with "Grant Denton". Would need custom resolution logic
3. **Sparse structural relationships**: Only 17 PRODUCED, 17 LED_TO, 14 DISCUSSED, 9 FOLLOWED_UP, 3 AFFECTS relationships -- the LLM extracted these less frequently
4. **Duplicate chunks**: Some meetings appear twice in Document nodes (70 documents for 50 meetings), likely from the same meeting ID having multiple instances
5. **No entity embeddings**: Only Chunk nodes have embeddings; entity nodes (Person, Client, etc.) do not, limiting vector search to chunk-level granularity

## Query Performance

| Query Type                   | Status     | Notes                                          |
| ---------------------------- | ---------- | ---------------------------------------------- |
| Vector Search (chunks)       | Working    | Uses chunk_embeddings index, cosine similarity |
| GraphRAG (answer generation) | Working    | Vector retrieval + GPT-4o-mini generation      |
| Text2Cypher                  | Working    | Generates correct Cypher from natural language |
| Raw Cypher                   | Working    | Direct graph traversal                         |
| Hybrid (VectorCypher)        | Not tested | Would need entity embeddings index             |

## Estimated Token Cost (50 meetings)

| Component               | Tokens     | Cost       |
| ----------------------- | ---------- | ---------- |
| LLM extraction (input)  | ~1,464,000 | $0.22      |
| LLM extraction (output) | ~488,000   | $0.29      |
| Embeddings              | ~488,000   | $0.01      |
| **Total**               |            | **~$0.52** |

Projected cost for all 1,477 meetings: ~$15.30

## Files Created

- `ingest_transcripts.py` - Main pipeline: loads meeting data, builds documents, feeds into SimpleKGPipeline
- `query_graph.py` - CLI query interface with vector search, Text2Cypher, GraphRAG, and raw Cypher
- `explore_graph.py` - Diagnostic script printing graph statistics and sample data
- `ingest_results.json` - Detailed results from the 50-meeting ingestion run
- `.env` - Environment variables (Neo4j connection, API keys)

## Architecture Notes

SimpleKGPipeline handles the full pipeline:

1. Text splitting (FixedSizeSplitter: 2000 chars, 200 overlap)
2. LLM-based entity/relationship extraction per chunk
3. Writing entities, relationships, and chunks to Neo4j
4. Embedding chunks with OpenAI text-embedding-3-small
5. Entity resolution (fuzzy matching)
6. Creates Document -> Chunk -> Entity graph structure with FROM_DOCUMENT, FROM_CHUNK, NEXT_CHUNK relationships

The library adds `__KGBuilder__` and `__Entity__` labels to all nodes it creates, alongside the domain-specific labels (Person, Client, etc.).

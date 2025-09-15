import logging
import uuid
import json
from abc import ABC, abstractmethod
from typing import Any, List, Dict, Optional, Union

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    retry_if_result,
)
from llama_index.core.chat_engine.types import ChatMode
from llama_index.core import (
    StorageContext,
    Document,
    PropertyGraphIndex,
    get_response_synthesizer,
    Settings,
)
from llama_index.core.llms import ChatMessage, LLM
from llama_index.core.schema import TransformComponent, TextNode
from llama_index.core.vector_stores import MetadataFilters, ExactMatchFilter
from llama_index.core.graph_stores.types import EntityNode, Relation

from llama_index.core.callbacks import CallbackManager, TokenCountingHandler
import traceback
from llama_index.core.response_synthesizers import (
    ResponseMode,
)
from llama_index.core.indices.property_graph import (
    SimpleLLMPathExtractor,
    ImplicitPathExtractor,
)


class BaseGraphIndexRagOps(ABC):
    """
    Abstract base class for RAG operations using LlamaIndex Property Graph.

    Provides methods for indexing, querying, and chat interactions
    with document collections using property graph structures.

    The class uses LlamaIndex's built-in query engine and chat engine patterns,
    allowing users to pass custom sub-retrievers for flexible retrieval strategies.

    Attributes:
        rag_index: Main property graph index for semantic search and graph traversal
        property_graph_store: Property graph store for graph storage
        vector_store: Optional vector store for similarity search
        storage_context: Storage context for data persistence
        emb_llm: Embedding language model
        completion_llm: Completion language model
        logger: Logger for debugging and monitoring
        kg_extractors: Knowledge graph extractors for entity/relation extraction
    """

    rag_index: Optional[PropertyGraphIndex] = None
    property_graph_store: Optional[Any] = None
    vector_store: Optional[Any] = None

    def __init__(
        self,
        completion_llm: LLM,
        emb_llm: Optional[LLM] = None,
        similarity_top_k: int = 6,
        response_mode: str = "tree_summarize",
        kg_extractors: Optional[List[TransformComponent]] = None,
        path_depth: int = 1,
        include_text: bool = True,
        embed_kg_nodes: bool = True,
    ):
        """Initialize with embedding and completion language models and configuration parameters.

        Args:
            emb_llm: Embedding language model
            completion_llm: Completion language model
            similarity_top_k: Number of top similar documents to retrieve (default: 3)
            response_mode: Response synthesis mode (default: "tree_summarize")
            kg_extractors: List of knowledge graph extractors (default: SimpleLLMPathExtractor + ImplicitPathExtractor)
            path_depth: Depth of relations to follow after node retrieval (default: 1)
            include_text: Whether to include source chunk text with retrieved paths (default: True)
            embed_kg_nodes: Whether to embed knowledge graph nodes (default: True)
        """
        self.emb_llm = emb_llm
        self.completion_llm = completion_llm
        self.similarity_top_k = similarity_top_k
        self.response_mode = response_mode
        self.path_depth = path_depth
        self.include_text = include_text
        self.embed_kg_nodes = embed_kg_nodes
        self.logger = logging.getLogger(__name__)
        self.token_counter = TokenCountingHandler()
        self._callback_manager = CallbackManager([self.token_counter])

        self._add_token_counter_to_llm(
            self.completion_llm
        )  # FOR token tracking in KG Extractors while creating the index

        # Set default knowledge graph extractors if not provided
        if kg_extractors is None:
            self.kg_extractors = [
                SimpleLLMPathExtractor(
                    llm=self.completion_llm,
                    max_paths_per_chunk=10,
                    num_workers=1,
                ),
                ImplicitPathExtractor(),
            ]
        else:
            self.kg_extractors = kg_extractors

    def _add_token_counter_to_llm(self, llm: LLM):
        if llm.callback_manager is None:
            llm.callback_manager = self._callback_manager
        else:
            # Add self.token_counter if not already present (by type)
            if not any(
                isinstance(h, TokenCountingHandler)
                for h in llm.callback_manager.handlers
            ):
                llm.callback_manager.add_handler(self.token_counter)

    def _get_response_mode(self) -> ResponseMode:
        """Convert string response mode to ResponseMode enum."""
        mode_mapping = {
            "tree_summarize": ResponseMode.TREE_SUMMARIZE,
            "simple_summarize": ResponseMode.SIMPLE_SUMMARIZE,
            "generation": ResponseMode.GENERATION,
            "refine": ResponseMode.REFINE,
            "compact": ResponseMode.COMPACT,
            "compact_accumulate": ResponseMode.COMPACT_ACCUMULATE,
            "accumulate": ResponseMode.ACCUMULATE,
        }

        if isinstance(self.response_mode, str):
            return mode_mapping.get(
                self.response_mode.lower(), ResponseMode.TREE_SUMMARIZE
            )
        return self.response_mode

    def add_kg_extractor(self, extractor: Any) -> None:
        """Add a knowledge graph extractor to the list of extractors.

        Args:
            extractor: Knowledge graph extractor to add
        """
        if self.kg_extractors is None:
            self.kg_extractors = []
        self.kg_extractors.append(extractor)

    def _create_metadata_filters(
        self, metadata_filter: Optional[Dict[str, str]] = None
    ) -> Optional[MetadataFilters]:
        """Create metadata filters from key-value pairs."""
        if not metadata_filter:
            return None

        filter_list = []
        for key, value in metadata_filter.items():
            filter_list.append(ExactMatchFilter(key=key, value=value))
        return MetadataFilters(filters=filter_list)

    def _create_documents_from_text_chunks(
        self, text_chunks: List[str], metadata: dict = None
    ) -> tuple[List[Document], List[str]]:
        """Create Document objects from text chunks with optional metadata."""
        if not text_chunks:
            raise ValueError("text_chunks cannot be empty")

        documents = []
        doc_ids = []

        for text in text_chunks:
            if not text.strip():  # Skip empty or whitespace-only chunks
                continue

            doc_id = f"doc_id_{uuid.uuid4()}"
            doc_chunk = Document(text=text, id_=doc_id)

            if metadata:
                doc_chunk.metadata = metadata.copy()

            documents.append(doc_chunk)
            doc_ids.append(doc_id)

        if not documents:
            raise ValueError("No valid text chunks provided after filtering")

        return documents, doc_ids

    def _log_retry_attempt(self, retry_state):
        """Log retry attempts with detailed information."""
        self.logger.warning(
            f"Retrying {retry_state.fn.__name__} after {retry_state.attempt_number} attempts. "
            f"Next attempt in {retry_state.next_action.sleep} seconds."
        )

    def _retry_on_empty_string_or_timeout_response(self, result) -> bool:
        """Check if response indicates failure requiring retry."""
        if hasattr(result, "response"):
            response_text = str(result.response)
        else:
            response_text = str(result)

        # Check for empty responses or known error patterns
        return (
            response_text == ""
            or response_text == "504.0 GatewayTimeout"
            or "timeout" in response_text.lower()
            or len(response_text.strip()) == 0
        )

    async def _query_with_retries(
        self,
        text_str: str,
        sub_retrievers: Optional[List[Any]] = None,
        metadata_filter: Optional[Dict[str, str]] = None,
    ):
        """Internal method with retry logic for robust querying."""

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=(
                retry_if_exception_type(Exception)
                | retry_if_result(
                    lambda result: self._retry_on_empty_string_or_timeout_response(
                        result
                    )
                )
            ),
            before_sleep=self._log_retry_attempt,
        )
        async def _aquery_with_retries():
            """Internal retry wrapper."""
            try:
                # Create query engine with token tracking and sub-retrievers
                query_engine_kwargs = {
                    "llm": self.completion_llm,
                    "response_synthesizer": get_response_synthesizer(
                        llm=self.completion_llm,
                        response_mode=self._get_response_mode(),
                        callback_manager=self._callback_manager,
                    ),
                    "include_text": self.include_text,
                    "similarity_top_k": self.similarity_top_k,
                }

                # Add sub_retrievers if provided, otherwise create default ones with correct LLM
                if sub_retrievers:
                    query_engine_kwargs["sub_retrievers"] = sub_retrievers
                else:
                    query_engine_kwargs["sub_retrievers"] = (
                        self._create_default_sub_retrievers(metadata_filter)
                    )

                query_engine = self.rag_index.as_query_engine(**query_engine_kwargs)

                # Generate response using the query engine
                response = await query_engine.aquery(text_str)
                return response
            except Exception as e:
                self.logger.error(traceback.format_exc())
                raise e

        return await _aquery_with_retries()

    async def query_index(
        self,
        text_str: str,
        sub_retrievers: Optional[List[Any]] = None,
        metadata_filter: Optional[Dict[str, str]] = None,
    ) -> Any:
        """
        Query the property graph index and generate a response using a query engine.

        Args:
            text_str: Main query for response generation
            retrieval_query: Optional separate query for document retrieval
            sub_retrievers: Optional list of sub-retrievers to use. If not provided,
                           LlamaIndex will use defaults (LLMSynonymRetriever and VectorContextRetriever)
            metadata_filter: Optional metadata filters for results (note: this parameter is kept
                           for backwards compatibility but filters should be configured in sub_retrievers)

        Returns:
            Generated response with context from retrieved documents and graph paths
        """
        if not self.rag_index:
            exists = await self.index_exists()
            if exists:
                await self.initiate_index()
            else:
                raise ValueError(
                    "No index exists. Create an index first using create_index()."
                )

        try:
            answer = await self._query_with_retries(
                text_str, sub_retrievers, metadata_filter
            )

            # Validate response quality
            if self._retry_on_empty_string_or_timeout_response(answer):
                raise ValueError(f"LLM RESPONSE IS NOT VALID: {answer}")

            return answer

        except Exception as e:
            self.logger.error(f"Query failed for text '{text_str[:50]}...': {e}")
            raise

    async def chat_with_index(
        self,
        curr_message: str,
        chat_history: List[ChatMessage],
        sub_retrievers: Optional[List[Any]] = None,
        metadata_filter: Optional[Dict[str, str]] = None,
    ) -> Any:
        """
        Engage in conversational interaction with the RAG index using a chat engine.

        Args:
            curr_message: Current user message
            chat_history: Previous messages for context
            sub_retrievers: Optional list of sub-retrievers to use. If not provided,
                           LlamaIndex will use defaults (LLMSynonymRetriever and VectorContextRetriever)

        Returns:
            Generated response considering conversation history
        """
        if not self.rag_index:
            exists = await self.index_exists()
            if exists:
                await self.initiate_index()
            else:
                raise ValueError(
                    "No index exists. Create an index first using create_index()."
                )

        try:
            # Create chat engine with context awareness
            chat_engine_kwargs = {
                "chat_mode": ChatMode.CONTEXT,
                "llm": self.completion_llm,
                "embed_model": self.emb_llm,
                "include_text": self.include_text,
                "similarity_top_k": self.similarity_top_k,
            }

            # Add sub_retrievers if provided, otherwise create default ones with correct LLM
            if sub_retrievers:
                chat_engine_kwargs["sub_retrievers"] = sub_retrievers
            else:
                chat_engine_kwargs["sub_retrievers"] = (
                    self._create_default_sub_retrievers(metadata_filter)
                )

            chat_engine = self.rag_index.as_chat_engine(**chat_engine_kwargs)
            if hasattr(chat_engine, "callback_manager"):
                chat_engine.callback_manager = self._callback_manager

            # Generate response with chat history context
            response = await chat_engine.achat(curr_message, chat_history)

            self.logger.debug(
                f"Chat response generated for message: {curr_message[:50]}..."
            )
            return response.response

        except Exception as e:
            self.logger.error(f"Chat failed for message '{curr_message[:50]}...': {e}")
            self.logger.error(traceback.format_exc())
            raise

    async def create_index(
        self,
        text_chunks: List[str],
        metadata: dict = None,
        transformations: List[TransformComponent] = None,
        kg_extractors: Optional[List[Any]] = None,
    ) -> List[str]:
        """
        Create a new property graph index from text chunks.

        Creates a new index object every time it is called, replacing any existing index.

        Args:
            text_chunks: List of text segments to index
            metadata: Optional metadata for all chunks
            transformations: Optional list of transformations to apply
            kg_extractors: Optional list of knowledge graph extractors to use

        Returns:
            List of document IDs for the indexed chunks
        """
        try:
            if not self.property_graph_store:
                await self.initiate_index()

            documents, doc_ids = self._create_documents_from_text_chunks(
                text_chunks, metadata
            )

            # Use provided kg_extractors or fallback to instance extractors
            extractors_to_use = kg_extractors or self.kg_extractors

            # Create a new property graph index from documents
            self.rag_index = PropertyGraphIndex.from_documents(
                documents,
                property_graph_store=self.property_graph_store,
                vector_store=self.vector_store,
                embed_model=self.emb_llm if self.embed_kg_nodes else None,
                kg_extractors=extractors_to_use,
                transformations=transformations,
                storage_context=self.storage_context,
                embed_kg_nodes=self.embed_kg_nodes,
                callback_manager=self._callback_manager,
            )

            self.logger.info(
                f"Created new property graph index with {len(documents)} documents"
            )

            # Persist the index using subclass-specific logic
            await self.persist_index()

            self.logger.info(f"Successfully indexed {len(documents)} documents")
            return doc_ids

        except Exception as e:
            self.logger.error(f"Failed to create property graph index: {e}")
            raise

    async def insert_text_chunks(
        self,
        text_chunks: List[str],
        metadata: dict = None,
        kg_extractors: Optional[List[Any]] = None,
        transformations: List[TransformComponent] = None,
    ) -> List[str]:
        """
        Insert text chunks into an existing property graph index.

        Args:
            text_chunks: List of text segments to insert
            metadata: Optional metadata for all chunks
            kg_extractors: Optional list of knowledge graph extractors to use

        Returns:
            List of document IDs for the inserted chunks
        """
        if not self.rag_index:
            raise ValueError("Index must be created before inserting text chunks")

        try:
            # Create documents from text chunks
            documents, doc_ids = self._create_documents_from_text_chunks(
                text_chunks, metadata
            )

            # Use provided kg_extractors or fallback to instance extractors
            extractors_to_use = kg_extractors or self.kg_extractors

            # Set transformations on the index if provided
            if extractors_to_use:
                self.rag_index._kg_extractors = extractors_to_use
            if transformations:
                self.rag_index._transformations = transformations

            # Insert documents
            for document in documents:
                self.rag_index.insert(document)

            self.logger.info(f"Successfully inserted {len(documents)} text chunks")

            # Persist the updated index
            await self.persist_index()

            return doc_ids

        except Exception as e:
            self.logger.error(f"Failed to insert text chunks: {e}")
            raise

    async def delete_documents(
        self,
        doc_ids: List[str],
    ) -> None:
        """
        Delete documents from the property graph index.

        Args:
            doc_ids: List of document IDs to delete
        """
        if not self.rag_index:
            raise ValueError("Index must be created before deleting documents")

        try:
            for doc_id in doc_ids:
                self.rag_index.delete(doc_id)

            self.logger.info(f"Successfully deleted {len(doc_ids)} documents")

            # Persist the updated index
            await self.persist_index()

        except Exception as e:
            self.logger.error(f"Failed to delete documents: {e}")
            raise

    @abstractmethod
    async def persist_index(self):
        """Persist the property graph index to storage backend."""
        pass

    @abstractmethod
    async def initiate_index(self):
        """Initialize the property graph index only if it already exists in the storage backend.

        This method should check if the index exists using index_exists(),
        and only instantiate an index object if it does exist.
        Otherwise, it should not create a new index.

        It should also initialize the property_graph_store and vector_store (if needed).
        """
        pass

    @abstractmethod
    async def index_exists(self) -> bool:
        """Check if the property graph index already exists in the storage backend."""
        pass

    # New non-abstract utility methods for property graph operations

    def from_existing_graph_store(
        self, property_graph_store: Any, vector_store: Optional[Any] = None, **kwargs
    ) -> None:
        """Create a property graph index from existing graph and vector stores.

        Args:
            property_graph_store: Existing property graph store
            vector_store: Optional existing vector store
            **kwargs: Additional arguments for PropertyGraphIndex.from_existing
        """
        try:
            self.property_graph_store = property_graph_store
            self.vector_store = vector_store

            # Update storage context
            if self.vector_store:
                self.storage_context = StorageContext.from_defaults(
                    property_graph_store=self.property_graph_store,
                    vector_store=self.vector_store,
                )
            else:
                self.storage_context = StorageContext.from_defaults(
                    property_graph_store=self.property_graph_store,
                )

            # Create index from existing stores
            self.rag_index = PropertyGraphIndex.from_existing(
                property_graph_store=self.property_graph_store,
                vector_store=self.vector_store,
                embed_model=self.emb_llm if self.embed_kg_nodes else None,
                embed_kg_nodes=self.embed_kg_nodes,
                callback_manager=self._callback_manager,
                **kwargs,
            )

            # Setup default sub-retrievers if not already set (kept for backwards compatibility)
            if not self.sub_retrievers:
                self.sub_retrievers = []

            self.logger.info("Successfully created index from existing graph store")

        except Exception as e:
            self.logger.error(f"Failed to create index from existing graph store: {e}")
            raise

    async def ingest_networkx_graph_nodes(
        self,
        networkx_graph_json: Union[str, Dict],
        content_field: str = "content",
    ) -> Dict[str, int]:
        """
        Ingest nodes and edges from a NetworkX graph JSON and convert to LlamaIndex objects.

        This function directly processes NetworkX graph JSON data and converts nodes/edges to
        LlamaIndex EntityNode, TextNode, and Relation objects for insertion into Neo4j and Qdrant.
        No KG extractors are used since all relationships are already present in the JSON.

        Args:
            networkx_graph_json: Either a JSON string or dictionary containing the NetworkX graph data
            content_field: The field name in each node that contains the text content for embeddings (default: "content")

        Returns:
            Dictionary containing:
                - "entity_nodes_added": Count of entity nodes that were created
                - "text_nodes_added": Count of text nodes that were created
                - "relations_added": Count of relation objects that were created

        Raises:
            ValueError: If the JSON structure is invalid or content field is missing
            KeyError: If required graph structure is not found
        """
        try:
            # Step 1: Parse and validate input JSON
            graph_data = self._parse_networkx_json(networkx_graph_json)
            nodes, edges = self._extract_nodes_and_edges(graph_data)

            # Step 2: Initialize index if needed
            if not self.property_graph_store:
                await self.initiate_index()

            # Step 3: Process nodes and create LlamaIndex objects
            entity_nodes, text_nodes = self._process_networkx_nodes(
                nodes, content_field
            )

            # Step 4: Process edges and create relation objects
            relations = self._process_networkx_edges(edges)

            self.logger.info(
                f"Created {len(entity_nodes)} entity nodes, {len(text_nodes)} text nodes, and {len(relations)} relations"
            )

            # Step 5: Store data in graph and vector stores
            self._store_entity_nodes_and_relations(entity_nodes, relations)
            self._store_text_nodes_with_embeddings(text_nodes)

            # Step 6: Create index and persist
            self.from_existing_graph_store(
                property_graph_store=self.property_graph_store,
                vector_store=self.vector_store if self.embed_kg_nodes else None,
            )
            await self.persist_index()

            self.logger.info(
                f"Successfully ingested NetworkX graph: {len(entity_nodes)} nodes, {len(relations)} relations"
            )

            return {
                "entity_nodes_added": len(entity_nodes),
                "text_nodes_added": len(text_nodes),
                "relations_added": len(relations),
            }

        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON format: {e}")
        except Exception as e:
            self.logger.error(f"Error ingesting NetworkX graph: {e}")
            raise e

    def _parse_networkx_json(self, networkx_graph_json: Union[str, Dict]) -> Dict:
        """Parse NetworkX JSON input into a dictionary."""
        if isinstance(networkx_graph_json, str):
            return json.loads(networkx_graph_json)
        return networkx_graph_json

    def _extract_nodes_and_edges(
        self, graph_data: Dict
    ) -> tuple[List[Dict], List[Dict]]:
        """Extract nodes and edges from NetworkX graph data structure."""
        try:
            networkx_data = graph_data["graph_data"]["networkx_data"]["directed"]
        except KeyError as e:
            missing_key = str(e).strip("'\"")
            raise KeyError(
                f"Required key '{missing_key}' not found in graph data structure. Expected path: graph_data['graph_data']['networkx_data']['directed']"
            )

        # Validate required keys exist
        if "nodes" not in networkx_data:
            raise KeyError("'nodes' key not found in the directed graph data")
        if "links" not in networkx_data:
            raise KeyError("'links' key not found in the directed graph data")

        nodes = networkx_data["nodes"]
        edges = networkx_data["links"]  # NetworkX JSON format uses "links" for edges

        if not nodes:
            raise ValueError("No nodes found in the graph data")

        return nodes, edges

    def _process_networkx_nodes(
        self,
        nodes: List[Dict],
        content_field: str,
    ) -> tuple[List[EntityNode], List[TextNode]]:
        """Convert NetworkX nodes to LlamaIndex EntityNode and TextNode objects."""
        # Define node field constants
        NODE_ID_FIELD = "id"
        NODE_TYPE_FIELD = "type"
        NODE_NAME_FIELD = "name"
        ENTITY_NODE_FIELDS = {NODE_ID_FIELD, NODE_TYPE_FIELD, NODE_NAME_FIELD}

        entity_nodes = []
        text_nodes = []

        for node in nodes:
            node_id = str(node.get(NODE_ID_FIELD, f"node_{uuid.uuid4()}"))
            entity_label = node.get(NODE_TYPE_FIELD, "entity")
            node_name = node.get(NODE_NAME_FIELD, "default_name")

            # Create entity properties (exclude content and core entity fields)
            entity_properties = self._extract_entity_properties(
                node, ENTITY_NODE_FIELDS
            )

            # Create EntityNode
            entity_node = EntityNode(
                label=entity_label,
                name=node_name,
                properties=entity_properties,
            )
            entity_nodes.append(entity_node)

            # Create TextNode if content exists and embedding is enabled
            text_node = self._create_text_node_if_valid(
                node, node_id, content_field, entity_properties
            )
            if text_node:
                text_nodes.append(text_node)

        return entity_nodes, text_nodes

    def _extract_entity_properties(
        self, node: Dict, excluded_fields: set
    ) -> Dict[str, str]:
        """Extract entity properties from node, excluding content and core fields."""
        entity_properties = {}

        for key, value in node.items():
            if key not in excluded_fields:
                if isinstance(value, (dict, list)):
                    entity_properties[key] = json.dumps(value)
                else:
                    entity_properties[key] = str(value)

        return entity_properties

    def _create_text_node_if_valid(
        self,
        node: Dict,
        node_id: str,
        content_field: str,
        entity_properties: Dict[str, str],
    ) -> Optional[TextNode]:
        """Create TextNode for embedding if content is valid and embedding is enabled."""
        # Check if content exists and embedding is enabled
        has_content = content_field in node and node[content_field]
        if not (has_content and self.embed_kg_nodes):
            self.logger.warning(
                f"Node {node_id} missing '{content_field}' field, skipping text node creation or self.embed_kg_nodes is False"
            )
            return None

        content = node[content_field]
        if not content.strip():
            self.logger.warning(
                f"Node {node_id} has empty '{content_field}' field, skipping text node creation"
            )
            return None

        # Create text node
        text_node = TextNode(id_=node_id, text=content, metadata=entity_properties)

        return text_node

    def _process_networkx_edges(self, edges: List[Dict]) -> List[Relation]:
        """Convert NetworkX edges to LlamaIndex Relation objects."""
        relations = []

        for edge in edges:
            source_id = str(edge.get("source", ""))
            target_id = str(edge.get("target", ""))

            if not source_id or not target_id:
                self.logger.warning(f"Edge missing source or target: {edge}")
                continue

            # Extract edge properties
            edge_properties = {
                "confidence": str(edge.get("confidence", "1.0")),
                "description": str(edge.get("description", "")),
            }

            relation_label = edge.get("relation_type", "related")
            relation = Relation(
                label=relation_label,
                source_id=source_id,
                target_id=target_id,
                properties=edge_properties,
            )
            relations.append(relation)

        return relations

    def _store_entity_nodes_and_relations(
        self, entity_nodes: List[EntityNode], relations: List[Relation]
    ) -> None:
        """Store entity nodes and relations in the property graph store."""
        if entity_nodes:
            self.property_graph_store.upsert_nodes(entity_nodes)
            self.logger.info(
                f"Inserted {len(entity_nodes)} entity nodes into property graph store"
            )

        if relations:
            self.property_graph_store.upsert_relations(relations)
            self.logger.info(
                f"Inserted {len(relations)} relations into property graph store"
            )

    def _store_text_nodes_with_embeddings(self, text_nodes: List[TextNode]) -> None:
        """Generate embeddings for text nodes and store in vector store."""
        if not (text_nodes and self.vector_store and self.emb_llm):
            return

        # Generate embeddings for text nodes
        for node in text_nodes:
            if hasattr(self.emb_llm, "get_text_embedding"):
                node.embedding = self.emb_llm.get_text_embedding(node.text)
            elif hasattr(self.emb_llm, "_get_text_embedding"):
                node.embedding = self.emb_llm._get_text_embedding(node.text)

        # Add text nodes to vector store
        self.vector_store.add(text_nodes)
        self.logger.info(
            f"Inserted {len(text_nodes)} text nodes with embeddings into vector store"
        )

    def _create_default_sub_retrievers(
        self, metadata_filter: Optional[Dict[str, str]]
    ) -> List[Any]:
        """Creates and returns a list of default sub-retrievers for property graph retrieval tasks.

        This method constructs sub-retrievers based on the configuration of the class instance and the provided metadata filter.
        It supports two types of retrievers:
            - LLMSynonymRetriever: Uses the completion LLM for synonym-based retrieval.
            - VectorContextRetriever: Uses an embedding model for vector-based retrieval, optionally filtered by metadata.

        Args:
            metadata_filter (Optional[Dict[str, str]]):
                A dictionary specifying metadata filters to apply when constructing vector-based retrievers.

            List[Any]:
                A list containing the instantiated sub-retrievers. The list may include:
                    - LLMSynonymRetriever (always included)
                    - VectorContextRetriever (included if embedding model and node embedding are enabled)
                If both embedding and metadata_filter are provided, only VectorContextRetriever with filters is returned because LLMSynonymRetriever cannot filter nodes.
        """
        from llama_index.core.indices.property_graph import (
            LLMSynonymRetriever,
            VectorContextRetriever,
        )

        def build_vector_context_retriever(filters=None):
            self._add_token_counter_to_llm(self.emb_llm)
            return VectorContextRetriever(
                graph_store=self.property_graph_store,
                embed_model=self.emb_llm,
                similarity_top_k=self.similarity_top_k,
                path_depth=self.path_depth,
                include_text=self.include_text,
                filters=filters,
            )

        default_sub_retrievers = []

        if self.embed_kg_nodes and self.emb_llm and metadata_filter:
            filters = self._create_metadata_filters(metadata_filter)
            default_sub_retrievers.append(build_vector_context_retriever(filters))
            return default_sub_retrievers

        default_sub_retrievers.append(
            LLMSynonymRetriever(
                graph_store=self.property_graph_store,
                llm=self.completion_llm,
                include_text=self.include_text,
                path_depth=self.path_depth,
            )
        )

        if self.embed_kg_nodes and self.emb_llm:
            default_sub_retrievers.append(build_vector_context_retriever())

        return default_sub_retrievers

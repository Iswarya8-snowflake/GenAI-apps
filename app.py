import os
import glob
import streamlit as st
import chromadb
from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings, ChatOllama
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_core.messages import SystemMessage, HumanMessage

# ==============================================================================
# CONFIGURATION
# ==============================================================================
EMBED_MODEL = "nomic-embed-text"
LLM_MODEL = "llama3.2:3b"
DATA_DIR = "data"
CHROMA_DIR = "chroma_db"
COLLECTION_NAME = "trading_platform_docs"
CHUNK_SIZE = 500
CHUNK_OVERLAP = 100
TOP_K = 4

# ==============================================================================
# PAGE CONFIGURATION
# ==============================================================================
st.set_page_config(
    page_title="ZenithTick RAG - Trading Platform Ops",
    page_icon="📈",
    layout="wide"
)

# ==============================================================================
# CORE RAG PIPELINE FUNCTIONS
# ==============================================================================

def load_and_chunk_documents():
    """
    Reads all markdown (.md) files from the data/ directory, chunks them into
    blocks of ~500 characters with overlap, and preserves source metadata.
    """
    documents = []
    md_files = glob.glob(os.path.join(DATA_DIR, "*.md"))
    
    for file_path in md_files:
        filename = os.path.basename(file_path)
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        # Attach the filename to metadata so we can cite it in answers
        documents.append(Document(page_content=content, metadata={"source": filename}))
    
    # Chunking: ~500 characters with overlap, splitting cleanly on markdown breaks
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n## ", "\n### ", "\n\n", "\n", ". ", " ", ""]
    )
    chunks = splitter.split_documents(documents)
    return chunks, len(md_files)


def get_vector_store():
    """
    Returns an instance of the persistent ChromaDB collection
    using Ollama's nomic-embed-text embeddings.
    """
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    embeddings = OllamaEmbeddings(model=EMBED_MODEL)
    return Chroma(
        client=client,
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings
    )


def reindex_documents():
    """
    Chunks all documents in data/ and embeds them into persistent ChromaDB,
    recreating the collection to ensure an up-to-date index.
    """
    chunks, file_count = load_and_chunk_documents()
    if not chunks:
        return 0, 0
    
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    
    # Safely clear old collection if it already exists
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    
    embeddings = OllamaEmbeddings(model=EMBED_MODEL)
    vector_store = Chroma(
        client=client,
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings
    )
    
    # Embed and add chunks to ChromaDB
    vector_store.add_documents(chunks)
    return file_count, len(chunks)


def answer_question(query: str, vector_store):
    """
    Retrieves the top 4 chunks from ChromaDB and answers using Ollama llama3.2:3b.
    Strictly instructs the model to only use the retrieved context.
    """
    # Retrieve top 4 relevant chunks
    retrieved_docs = vector_store.similarity_search(query, k=TOP_K)
    
    # Extract unique source filenames
    sources = sorted(list({doc.metadata.get("source", "Unknown") for doc in retrieved_docs}))
    
    # Combine retrieved chunks into context
    context_text = "\n\n---\n\n".join([doc.page_content for doc in retrieved_docs])
    
    # System prompt enforcing context-only answers
    system_prompt = (
        "You are an operations assistant for the ZenithTick trading data platform.\n"
        "Answer the user's question using ONLY the retrieved context provided below.\n"
        "If the context does not contain enough information to answer the question, "
        "state: 'The provided context does not contain enough information to answer this question.'\n"
        "Do not make up facts or use external knowledge not present in the context."
    )
    
    user_prompt = (
        f"Context:\n{context_text}\n\n"
        f"Question: {query}\n\n"
        "Answer:"
    )
    
    llm = ChatOllama(model=LLM_MODEL, temperature=0.0)
    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt)
    ])
    
    return response.content, sources, retrieved_docs


# ==============================================================================
# STREAMLIT UI: SIDEBAR & HEADER
# ==============================================================================

st.sidebar.title("⚙️ System Configuration")
st.sidebar.markdown(f"- **LLM Model:** `{LLM_MODEL}`")
st.sidebar.markdown(f"- **Embedding Model:** `{EMBED_MODEL}`")
st.sidebar.markdown(f"- **Vector Store:** `{CHROMA_DIR}/`")
st.sidebar.markdown(f"- **Retrieved Chunks (k):** `{TOP_K}`")

# Connect to the vector store
vector_store = get_vector_store()

# Check current chunk count in ChromaDB
try:
    total_chunks = vector_store._collection.count()
except Exception:
    total_chunks = 0

st.sidebar.metric(label="Indexed Chunks in ChromaDB", value=total_chunks)

# Sidebar button to re-index documents
if st.sidebar.button("🔄 Re-index Documents", type="primary"):
    with st.spinner("Re-indexing markdown files into ChromaDB..."):
        num_files, num_chunks = reindex_documents()
    st.sidebar.success(f"Indexed {num_chunks} chunks from {num_files} documents!")
    st.rerun()

# Automatically index on first start if ChromaDB is empty
if total_chunks == 0:
    st.info("ChromaDB collection is empty. Performing initial document indexing...")
    with st.spinner("Indexing documents from data/..."):
        num_files, num_chunks = reindex_documents()
    st.success(f"Initial indexing complete: {num_chunks} chunks from {num_files} files.")
    st.rerun()

# App Header
st.title("📈 Trading Data Platform - Incident & Runbook RAG")
st.caption("Ask questions about ZenithTick incident reports, runbooks, diagnosis steps, and resolutions.")

# ==============================================================================
# CHAT INTERFACE & SESSION STATE
# ==============================================================================

# Initialize message history
if "messages" not in st.session_state:
    st.session_state.messages = []

# Render previous messages from history
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message.get("sources"):
            st.markdown("**Source Files:**")
            for src in message["sources"]:
                st.markdown(f"- `{src}`")

# Chat input box for user queries
user_input = st.chat_input("Ask a question about incidents, runbooks, or platform operations...")

if user_input:
    # Append and show user message
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Generate assistant answer
    with st.chat_message("assistant"):
        with st.spinner("Retrieving relevant context and generating answer..."):
            try:
                answer, sources, retrieved_chunks = answer_question(user_input, vector_store)
                
                # Display the model's answer
                st.markdown(answer)
                
                # Show the source file names under the answer
                if sources:
                    st.markdown("**Source Files:**")
                    for src in sources:
                        st.markdown(f"- `{src}`")
                
                # Expandable view of the retrieved chunks for transparency
                with st.expander("🔍 View Retrieved Context Chunks"):
                    for idx, chunk in enumerate(retrieved_chunks, start=1):
                        st.markdown(f"**Chunk {idx}** | Source: `{chunk.metadata.get('source', 'Unknown')}`")
                        st.text(chunk.page_content)
                        st.divider()

                # Save assistant response to session state
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": answer,
                    "sources": sources
                })
            except Exception as ex:
                err_msg = f"⚠️ An error occurred: {str(ex)}"
                st.error(err_msg)
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": err_msg,
                    "sources": []
                })
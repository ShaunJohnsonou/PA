# Functional Requirements & Pricing Requirements — LEAF Document Intelligence Assistant

## Document purpose (not a marketing document)
This document is a *functional/technical requirements specification* describing what the LEAF (Leaf Excellence in Auditing Forum / Leaf Quality Systems) Document Intelligence Assistant is, what it can do, and how pricing/usage measurement is defined.

It is written so it can be shared with *LEAF* and with *other firms with a similar business model* (e.g., auditor communities, audit support networks, professional services groups)
---

## 1) Scope
The system provides a personal assistant accessible via Telegram that helps audit- and assurance-related teams:
- Store and manage documents securely.
- Easily retrieve stored documents and specific information.
- Read and analyze documents to provide insights, summaries, and answers to questions.
- Interact naturally through a conversational chat interface.
- Understand and control cost via metered pay-per-use pricing.
---

## 2) Primary users & usage scenarios
### 2.1 Primary users (for LEAF)
- LEAF specialists supporting member firms (technical support)
- LEAF administrators configuring deployments and billing
- Member-firm staff consuming guidance, extracting insights (AI insights must be verified.), and storing reference documents

### 2.2 Representative scenarios
1. *Document Storage & Retrieval:* A user uploads a reference document or requests an old one. The assistant stores it securely and retrieves it instantly upon request.
2. *Document-Assisted Q&A:* A user asks a specific question about a previously stored policy document. The assistant reads the document and provides a concise, accurate answer based on the text.
3. *Insight Extraction:* The user asks the assistant to summarize a lengthy document or extract key takeaways.
4. *Chat-Based Guidance:* A user asks a general question, and the assistant responds within the chat thread.
5. RAG can be included to enhance retrieving context relevant documents
---

## 3) System overview
### 3.1 High-level capabilities
The system includes the following core functional modules:
- *Ingestion Module:* Accepts documents (PDFs, Excel, CSV, Word) via direct uploads or monitored directories.
- *Storage Module:* Securely stores documents and tracks metadata using an internal SQLite database.
- *Indexing & Retrieval Module:* Uses semantic search (RAG) to find relevant document content quickly.
- *Analysis Module:* Reads retrieved documents to provide insights, summaries, and answers.
- *Chat Interface Module:* Provides a seamless messaging experience via Telegram.

### 3.2 Interface requirement: Telegram integration
The assistant provides a *Telegram chat experience* comparable to familiar messaging workflows:
- Users can send messages and documents directly in the chat.
- The assistant responds in-chat with fast, conversational outputs.
- Conversational context is maintained per session.
---

## 4) Functional requirements
### 4.1 Document ingestion & storage
*FR-1 (Secure ingestion):*
- Accept common document types required by LEAF’s workflows (e.g., PDF, Word, Excel, CSV).

*FR-2 (Metadata capture):*
- Store document metadata (filename, storage timestamp, category, status) in a local database to ensure easy retrieval.

### 4.2 Indexing & retrieval
*FR-3 (Index creation):*
- Generate an internal vector index for documents to enable fast, semantic search and retrieval.

*FR-4 (Grounded answers):*
- When answering questions about documents, responses must be grounded strictly in the retrieved text.

### 4.3 Analysis & output generation
*FR-5 (Insight generation):*
The assistant must support generating outputs such as:
- Document summaries and key takeaways.
- Direct answers to user queries based on document content.

*FR-6 (Iterative interaction):*
- Support follow-up prompts in the same Telegram session, maintaining conversational context.

### 4.4 Static vetted model
*FR-7 (Static Model Guarantee):*
- The assistant uses a *single vetted/static model* (e.g., Azure OpenAI) for responses.
- The model is validated to ensure it performs correctly for document Q&A.
- No user-controlled backend model switching is performed.

### 4.5 Multi-firm support (LEAF + similar organizations)
*FR-8 (Tenant separation):*
- Support separate, isolated deployments per firm (e.g., via distinct, containerized Docker instances).
- Ensure document isolation, database separation, and access isolation between firms.
---

## 5) Security, privacy, and compliance requirements
*SR-1 (Secure-by-design deployment):*
- All services run inside isolated Docker containers.

*SR-2 (Data protection & isolation):*
- Documents and databases (SQLite) are stored locally on the tenant's isolated volume. No data is shared between tenants.
- Use TLS/HTTPS for communications with the Telegram API and the LLM provider.

*SR-3 (Access control):*
- Enforce access controls by restricting the Telegram bot to a whitelist of authorized User IDs. Unauthorized users cannot interact with the bot.

*SR-4 (Credential handling):*
- Ensure API keys and tokens are passed securely via environment variables and are never hardcoded.
---

## 6) Deployment model
### 6.1 Deployment options
- *Option A:* Hosted deployment managed by the provider (e.g., dedicated Azure Virtual Machines per tenant).
- *Option B:* Customer-managed/self-hosted deployment via Docker containers.

### 6.2 Architecture requirements
- The system uses a self-contained containerized architecture ensuring high portability, strict data isolation, and ease of setup.
---

## 7) Usage metering & pay-per-use pricing requirements
The commercial model requested is:
- *Flat fee* for server hosting/coverage
- *One-time secure setup fee*
- *Pay-per-use billing* for usage thereafter

### 7.1 Billing line items
*CR-1 (Hosting flat fee):*
- Covers compute capacity, storage baseline, availability, and operational overhead.

*CR-2 (Secure setup fee):*
- Covers secure configuration, tenant isolation setup, access control configuration, and deployment hardening.

*CR-3 (Usage fee):*
- Usage is metered based on measurable units (e.g., token consumption or per-query counts logged by the system).

### 7.2 Recommended “usage unit” definitions
- *Per analysis job:* Each assistant response that triggers retrieval and analysis counts as a job. Follow-ups count as separate jobs.

### 7.3 Pricing parameters (placeholders)
Replace placeholders with your final rates:
- Hosting flat fee: *[INSERT $/month or $/quarter]*
- Secure setup fee: *[INSERT one-time $]*
- Usage price:
  - per analysis job: *[INSERT $/job]* 
---

## 8) Packaging / plans (optional)
If plans are offered, define them as operational constraints:
- Allowed storage capacity per tenant.
- Support response time targets.
---

## 9) Acceptance checklist (for finalizing for LEAF)
To finalize this document for LEAF distribution, complete:
1. Finalize supported ingestion file types.
2. Definition of an “analysis job” for billing.
3. Final usage unit(s) and rates.
4. Confirmation of Telegram workflow requirements.
5. Finalize hosting and setup fees.

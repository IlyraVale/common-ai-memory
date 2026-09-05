# Shared Memory

memory_store.py is the production Markdown store. A record contains ID, owner, scope (agent or shared), category, timestamps, and content. All configured AI identities may read records; only the owner may update or forget them. owner=shared filters by scope rather than author.

Writes use a process-exclusive lock file and atomic replacement. Different MemoryStore instances and processes therefore serialize mutations. If DATA_DIR itself is a Git worktree, a mutation attempts a local commit; failure does not discard the memory write and nothing is pushed.

Categories are validated as section/subject. The open-source fallback policy contains generic relationship, project, game, life, preference, learning, and plan categories. No production memory or private category index is included.

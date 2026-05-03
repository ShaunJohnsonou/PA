# Document Storage

Place your personal documents here. This directory is git-ignored.

## Suggested Structure

```
document_storage/
├── Personal/
│   ├── Finance/
│   │   └── Statements/
│   ├── Health/
│   ├── Home/
│   ├── Legal/
│   ├── Photos/
│   └── Projects/
└── Work/
    ├── Misc/
    ├── Projects/
    └── Reports/
```

The Hermes agent can read, catalogue, and query these documents using the `read_doc.py` utility and the `documents` PostgreSQL table.

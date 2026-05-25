import time
t0 = time.time()
from docling.document_converter import DocumentConverter
t1 = time.time()
print(f"import: {t1-t0:.1f}s")
c = DocumentConverter()
t2 = time.time()
print(f"init: {t2-t1:.1f}s")
r = c.convert("/hermes-vault/originals/2b/d3/2bd385db02bdb90b965c27eda2e17f93c0c418a1ee1199ec51cbc18b2941ec39.pdf")
t3 = time.time()
print(f"convert: {t3-t2:.1f}s total: {t3-t0:.1f}s")
md = r.document.export_to_markdown()
print(f"md: {len(md)} chars")

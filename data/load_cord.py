"""Optional: `python data/load_cord.py`

Downloads the CORD receipts dataset from Hugging Face (verify the dataset ID, likely
naver-clova-ix/cord-v2), maps its line items, subtotal, tax and total to our schema, and writes
images to data/docs/ and labels to data/labels/. Fields CORD does not have are skipped.
"""

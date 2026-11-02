"""Streamlit labeling tool: `streamlit run label_app.py`

Shows a document from data/docs/ next to an editable form pre-filled by ESCALATION_MODEL's
extraction. A human verifies or corrects every field and saves ground truth to
data/labels/<doc_id>.json, tracking which fields were corrected.
"""

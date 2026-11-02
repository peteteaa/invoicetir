"""Load PDFs and images.

If a PDF has a usable text layer (PyMuPDF), return its text with page markers for TEXT_MODEL;
otherwise render the pages to images for VISION_MODEL.
"""

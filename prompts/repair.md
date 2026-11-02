Your previous answer failed these checks:

{failures}

Look at the document again and return the corrected JSON object, the whole object, in the same
format.

- Copy printed values exactly. Never change a number just to make the math add up.
- The usual causes are a misread digit, a skipped or duplicated row, a wrapped description
  split into two items, or a gross (tax-included) amount used where the net amount belongs.
- If you re-read the document and its own printed numbers really don't add up, return them as
  printed and list the affected fields in `uncertain_fields`.

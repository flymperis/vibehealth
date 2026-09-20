"""Numbers shared by the upload code (in the server) and the sandbox child (which parses the file).

Only constants and no imports: the child imports this, and it must stay light.
"""

MAX_PDF_PAGES = 40  # a lab report is a few pages: each page is two model calls
MAX_PDF_PAGE_POINTS = 2000  # 28 in: bigger than A2. A page size is what a 150 dpi render is made from
MAX_IMAGE_PIXELS = 48_000_000  # 8000 x 6000: a 48 MP phone photo. Decoded RGB is 3 bytes a pixel
MAX_IMAGE_SIDE = 16_384
THUMB_SIDE = 400

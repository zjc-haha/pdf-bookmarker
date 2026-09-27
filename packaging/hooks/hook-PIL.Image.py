"""The application only opens and writes PNG images for PDFium previews.

PyInstaller's default Pillow hook includes every optional image codec, even
though those formats are never handed to Pillow by this application.
"""

hiddenimports = ["PIL.PngImagePlugin"]

# Third-party components in the Windows portable ZIP

Each component keeps its own license. The corresponding license texts are in
the ZIP's `licenses/` directory.

| Runtime component | Version | License file or directory |
| --- | --- | --- |
| [CPython](https://www.python.org/) and its standard library | 3.12.14 | `CPython-LICENSE.txt` |
| Tcl/Tk GUI runtime | 8.6.12 | `Tcl-Tk-license.terms.txt` |
| OpenSSL DLLs used by Python | 3.5.8 | `OpenSSL-LICENSE.txt` |
| libffi DLL and cffi backend | libffi version not recorded; cffi 2.1.1 | `libffi-LICENSE.txt`, `cffi-LICENSE.txt` |
| [pypdf](https://github.com/py-pdf/pypdf) | 6.10.0 | `pypdf-LICENSE.txt` |
| [pdfplumber](https://github.com/jsvine/pdfplumber) | 0.11.9 | `pdfplumber-LICENSE.txt` |
| [pdfminer.six](https://github.com/pdfminer/pdfminer.six) | 20251230 | `pdfminer.six-LICENSE.txt` |
| [Pillow](https://github.com/python-pillow/Pillow) | 12.3.0 | `Pillow-LICENSE.txt` |
| [Tabler Icons Outline](https://github.com/tabler/tabler-icons) | 3.48.0 (selected icons) | `Tabler-Icons-LICENSE.txt` |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) and PDFium | pypdfium2 5.13.0; PDFium 153.0.7999.0 | `pypdfium2/` (includes PDFium and statically linked dependency notices) |
| [cryptography](https://github.com/pyca/cryptography) | 50.0.1 | `cryptography-LICENSE*.txt` |
| [charset-normalizer](https://github.com/jawah/charset_normalizer) | 3.5.1 | `charset-normalizer-LICENSE.txt` |
| [PyInstaller](https://pyinstaller.org/) bootloader and runtime hooks | 6.22.3 | `PyInstaller-COPYING.txt` |
| [pyinstaller-hooks-contrib](https://github.com/pyinstaller/pyinstaller-hooks-contrib) runtime hooks | 2026.7 | `PyInstaller-hooks-contrib-LICENSE.txt` |

The PDFium binary is from the `pypdfium2` Windows x64 wheel. The wheel's
`LICENSES` and `BUILD_LICENSES` files are reproduced under `licenses/pypdfium2/`.
The wheel metadata identifies the binary origin as `pdfium-binaries` and
PDFium 153.0.7999.0. The source projects are
[PDFium](https://pdfium.googlesource.com/pdfium/) and
[pdfium-binaries](https://github.com/bblanchon/pdfium-binaries).

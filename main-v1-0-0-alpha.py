"""
Lean PDF Flattener (Python + Flet 0.28)
---------------------------------------
- Instant PDF preview on upload
- Live watermark preview (debounced)
- Page navigation (Prev / Next)
- Bulk flattening with progress bars
- Light-mode UI
"""

from __future__ import annotations
import io, os, base64, threading, tempfile
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass

import flet as ft
import logging

logging.basicConfig(
    filename="pdf_flattener.log",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

from PIL import Image, ImageDraw, ImageFont
import fitz  # PyMuPDF


# ---------------- Utilities ----------------
SYSTEM_SANS = [
    "C:\\Windows\\Fonts\\segoeui.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

def find_font() -> Optional[str]:
    for path in SYSTEM_SANS:
        if os.path.exists(path):
            return path
    return None


@dataclass
class WatermarkSpec:
    text: str
    size_pct: float
    tile_px: int
    opacity: int


@dataclass
class FlattenOptions:
    dpi: int
    jpeg_quality: int
    target_size_mb: float
    export_format: str
    watermark: WatermarkSpec


# ---------------- PDF helpers ----------------
def render_page(doc, page_index: int, dpi: int) -> Image.Image:
    page = doc.load_page(page_index)
    zoom = max(1/4, dpi / 72)  # guard against 0 / bad values
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def apply_watermark(img: Image.Image, wm: WatermarkSpec) -> Image.Image:
    # Always return a valid image; never raise
    try:
        if not wm.text:
            return img
        font_size = max(8, int(img.width * max(0.01, wm.size_pct) / 100))
        font_path = find_font()
        try:
            font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        fill = (180, 180, 180, max(0, min(255, wm.opacity)))
        step = max(10, int(wm.tile_px))

        # Tiled text
        for y in range(0, img.height + step, step):
            for x in range(0, img.width + step, step):
                draw.text((x, y), wm.text, fill=fill, font=font)

        composed = Image.alpha_composite(img.convert("RGBA"), overlay)
        return composed.convert("RGB")
    except Exception:
        # Fallback: return original if anything goes wrong
        return img


def encode_image(img: Image.Image, fmt: str, quality: int) -> bytes:
    buf = io.BytesIO()
    if fmt.lower() == "jpeg":
        img.save(buf, format="JPEG", quality=max(1, min(100, quality)))
    else:
        img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def save_pdf(images: List[Image.Image], out_path: Path, quality: int):
    pdf = fitz.open()
    try:
        for img in images:
            data = encode_image(img, "jpeg", quality)
            rect = fitz.Rect(0, 0, img.width, img.height)
            page = pdf.new_page(width=img.width, height=img.height)
            page.insert_image(rect, stream=data)
        pdf.save(out_path)
    finally:
        pdf.close()


def process_pdf(in_path: Path, out_dir: Path, opts: FlattenOptions, on_progress=None):
    outputs = []
    with fitz.open(in_path) as doc:
        total = doc.page_count
        images = []
        for i in range(total):
            img = render_page(doc, i, opts.dpi)
            img = apply_watermark(img, opts.watermark)
            images.append(img)
            if on_progress:
                try:
                    on_progress(i + 1, total)
                except Exception:
                    pass

    stem = in_path.stem
    if opts.export_format == "pdf":
        out = out_dir / f"{stem}_flattened.pdf"
        save_pdf(images, out, opts.jpeg_quality)
        outputs.append(out)
    else:
        for idx, img in enumerate(images, start=1):
            out = out_dir / f"{stem}_{idx:03d}.{opts.export_format}"
            data = encode_image(img, opts.export_format, opts.jpeg_quality)
            with open(out, "wb") as f:
                f.write(data)
            outputs.append(out)
    # free PIL images
    for im in images:
        try: im.close()
        except: pass
    return outputs


# ---------------- Main UI ----------------
class PDFToolApp(ft.Column):
    def __init__(self, page: ft.Page):
        super().__init__(expand=True, scroll=ft.ScrollMode.AUTO)
        self.page = page
        self.selected_files: List[Path] = []
        self.output_dir = Path.home() / "PDF_Flattener_Output"
        self.output_dir.mkdir(exist_ok=True)
        self._preview_doc = None
        self._preview_page_index = 0
        self._preview_debounce = None  # threading.Timer

        # Pickers
        self.pick_files = ft.FilePicker(on_result=self._on_files)
        self.pick_folder = ft.FilePicker(on_result=self._on_folder)
        page.overlay.extend([self.pick_files, self.pick_folder])

        # File controls
        self.files_list = ft.ListView(expand=True, height=160, spacing=6)
        self.add_btn = ft.ElevatedButton("Add PDFs…", on_click=self._pick_files)
        self.out_btn = ft.TextButton("Change output folder", on_click=self._pick_folder)
        self.output_label = ft.Text(f"Output: {self.output_dir}")

        # Watermark controls
        self.wm_text = ft.TextField(label="Watermark text")
        self.wm_size = ft.Slider(label="Font size %", min=2, max=20, value=6, divisions=18)
        self.wm_tile = ft.Slider(label="Tile spacing (px)", min=50, max=600, value=200, divisions=22)
        self.wm_opacity = ft.Slider(label="Opacity", min=30, max=255, value=120, divisions=45)

        # Quality
        self.dpi = ft.Slider(label="DPI", min=72, max=600, value=150, divisions=528)
        self.quality = ft.Slider(label="JPEG Quality", min=1, max=100, value=85, divisions=99)
        self.target = ft.TextField(label="Target size (MB, optional)", value="")
        self.format = ft.Dropdown(
            label="Export format",
            options=[ft.dropdown.Option("pdf"), ft.dropdown.Option("png"), ft.dropdown.Option("jpeg")],
            value="pdf",
        )

        # Progress
        self.file_label = ft.Text(visible=False)
        self.file_progress = ft.ProgressBar(width=400, visible=False)
        self.overall_label = ft.Text(visible=False)
        self.overall_progress = ft.ProgressBar(width=400, visible=False)

        # Preview
        self.preview_image = ft.Image(width=420, height=560, fit=ft.ImageFit.CONTAIN, visible=False)
        self.nav_controls = ft.Row([
            ft.IconButton(ft.Icons.ARROW_BACK, on_click=self._prev_page),
            ft.Text("Page", size=14),
            ft.IconButton(ft.Icons.ARROW_FORWARD, on_click=self._next_page),
        ], alignment=ft.MainAxisAlignment.CENTER)

        self.process_btn = ft.FilledButton("Flatten & Export", on_click=self._process)
        self.clear_btn = ft.TextButton("Clear", on_click=self._clear)

        # Layout
        self.controls = [
            ft.Text("Lean PDF Flattener", size=20, weight=ft.FontWeight.BOLD),
            ft.Row([self.add_btn, self.out_btn]),
            self.output_label,
            ft.Text("Queued files:"), self.files_list,
            ft.Divider(),
            ft.Text("Watermark Options"),
            self.wm_text,
            ft.Row([self.wm_size, self.wm_tile, self.wm_opacity]),
            ft.Divider(),
            ft.Text("Quality Settings"),
            ft.Row([self.dpi, self.quality]),
            ft.Row([self.format, self.target]),
            ft.Divider(),
            ft.Column([self.preview_image, self.nav_controls]),
            ft.Divider(),
            ft.Column([
                self.file_label, self.file_progress,
                self.overall_label, self.overall_progress,
                ft.Row([self.process_btn, self.clear_btn])
            ])
        ]

        # Live preview binding (debounced)
        for ctrl in [self.wm_text, self.wm_size, self.wm_tile, self.wm_opacity, self.dpi]:
            ctrl.on_change = self._debounced_update_preview

    # ---------- File ops ----------
    def _pick_files(self, e): 
        self.pick_files.pick_files(allow_multiple=True, allowed_extensions=["pdf"])

    def _on_files(self, e):
        if e.files:
            for f in e.files:
                path = Path(f.path)
                if path.exists() and path not in self.selected_files:
                    self.selected_files.append(path)
                    self.files_list.controls.append(ft.Text(str(path)))
            self.page.update()
            if self.selected_files:
                # Load preview immediately (no async)
                self._load_pdf_preview(self.selected_files[0])

    def _pick_folder(self, e): 
        self.pick_folder.get_directory_path()

    def _on_folder(self, e):
        if e.path:
            self.output_dir = Path(e.path)
            self.output_label.value = f"Output: {self.output_dir}"
            self.page.update()

    def _clear(self, e=None):
        self.selected_files.clear()
        self.files_list.controls.clear()
        self.preview_image.visible = False
        # Close any open preview doc
        if self._preview_doc:
            try: self._preview_doc.close()
            except: pass
        self._preview_doc = None
        self.page.update()

    # ---------- Flatten ----------
    def _process(self, e):
        if not self.selected_files:
            self._show_message("No files selected")
            return
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        wm = WatermarkSpec(
            self.wm_text.value.strip(),
            float(self.wm_size.value),
            int(self.wm_tile.value),
            int(self.wm_opacity.value),
        )
        try:
            target_mb = float(self.target.value.strip()) if self.target.value.strip() else 0.0
        except:
            target_mb = 0.0
        opts = FlattenOptions(
            dpi=int(self.dpi.value),
            jpeg_quality=int(self.quality.value),
            target_size_mb=target_mb,
            export_format=self.format.value,
            watermark=wm,
        )
        total = len(self.selected_files)
        self._show_progress(True)
        for i, file in enumerate(self.selected_files, start=1):
            self._file_label(f"Processing {file.name} ({i}/{total})")
            def per_page(cur, tot): self._file_prog(cur / max(1, tot))
            process_pdf(file, self.output_dir, opts, on_progress=per_page)
            self._overall_prog(i / total)
        self._done()

    # ---------- Preview core ----------
    def _load_pdf_preview(self, path: Path):
        try:
            if not path.exists():
                self._show_message(f"File not found: {path}")
                return
            if self._preview_doc:
                try: self._preview_doc.close()
                except: pass
            self._preview_doc = fitz.open(str(path))
            self._preview_page_index = 0
            self._update_preview_image()
        except Exception as ex:
            self._show_message(f"Failed to load PDF: {ex}")

    def _update_preview_image(self, e=None):
        """Render preview safely using a temp file (no base64)."""
        try:
            if not self._preview_doc:
                return

            page_index = max(0, min(self._preview_page_index, len(self._preview_doc) - 1))
            doc = self._preview_doc

            wm = WatermarkSpec(
                self.wm_text.value or "",
                float(self.wm_size.value or 6),
                int(self.wm_tile.value or 200),
                int(self.wm_opacity.value or 120),
            )

            img = render_page(doc, page_index, int(self.dpi.value or 150))
            img = apply_watermark(img, wm)

            # Downscale for UI
            max_w = 960
            if img.width > max_w:
                ratio = max_w / img.width
                img = img.resize((int(img.width * ratio), int(img.height * ratio)))

            # Save to temp file instead of base64
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            img.save(tmp.name, "JPEG", quality=70, optimize=True)
            img.close()

            # Display directly from file
            self.preview_image.src = tmp.name
            self.preview_image.visible = True
            self.page.update()

            log.debug(f"Preview image updated from temp file: {tmp.name}")

        except Exception as ex:
            log.exception("Preview failed")
            self._show_message(f"Preview failed: {ex}")




    # Debounce typing/slider changes to avoid crashes while you type fast
    def _debounced_update_preview(self, e=None):
        try:
            if self._preview_debounce:
                self._preview_debounce.cancel()
        except Exception:
            pass
        def run():
            self._update_preview_image()
        self._preview_debounce = threading.Timer(0.2, run)
        self._preview_debounce.start()

    def _next_page(self, e):
        if self._preview_doc and self._preview_page_index < len(self._preview_doc) - 1:
            self._preview_page_index += 1
            self._update_preview_image()

    def _prev_page(self, e):
        if self._preview_doc and self._preview_page_index > 0:
            self._preview_page_index -= 1
            self._update_preview_image()

    # ---------- Progress ----------
    def _show_progress(self, visible):
        self.file_label.visible = visible
        self.file_progress.visible = visible
        self.overall_label.visible = visible
        self.overall_progress.visible = visible
        self.page.update()

    def _file_label(self, text):
        self.file_label.value = text
        self.page.update()

    def _file_prog(self, f):
        self.file_progress.value = max(0.0, min(1.0, f))
        self.page.update()

    def _overall_prog(self, f):
        self.overall_progress.value = max(0.0, min(1.0, f))
        self.overall_label.value = f"Overall: {int(self.overall_progress.value*100)}%"
        self.page.update()

    def _done(self):
        self.file_label.value = "Done!"
        self._show_message("All files processed")

    # ---------- Snackbar ----------
    def _show_message(self, text):
        self.page.snack_bar = ft.SnackBar(ft.Text(text))
        self.page.snack_bar.open = True
        self.page.update()


# ---------------- Entry point ----------------
def main(page: ft.Page):
    page.title = "Lean PDF Flattener"
    page.window_width = 980
    page.window_height = 780
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 16
    page.scroll = "adaptive"
    app = PDFToolApp(page)
    page.add(app)

if __name__ == "__main__":
    ft.app(target=main)

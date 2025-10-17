from __future__ import annotations
import io, os, base64, threading, tempfile, time, logging
from pathlib import Path
from typing import List, Optional, Tuple
from dataclasses import dataclass

import flet as ft
from PIL import Image, ImageDraw, ImageFont
import fitz  # PyMuPDF

# ---------------- Logging ----------------
logging.basicConfig(
    filename="pdf_flattener.log",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------- Utilities ----------------
SYSTEM_SANS = [
    "C:\\Windows\\Fonts\\segoeui.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

def find_font() -> Optional[str]:
    for path in SYSTEM_SANS:
        if os.path.exists(path):
            return path
    return None

def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> Tuple[int, int]:
    # Reliable sizing with bbox
    bbox = draw.textbbox((0, 0), text, font=font)
    w = max(1, bbox[2] - bbox[0])
    h = max(1, bbox[3] - bbox[1])
    return w, h

@dataclass
class WatermarkSpec:
    text: str
    size_pct: float     # relative to page width
    opacity: int        # 0..255
    tile_padding_pct: float  # padding between tiles, relative to font size (both axes)

@dataclass
class FlattenOptions:
    dpi: int
    jpeg_quality: int
    export_format: str  # pdf/png/jpeg
    watermark: WatermarkSpec
    tiled: bool
    rotate_45: bool

# ---------------- PDF helpers ----------------
def render_page(doc, page_index: int, dpi: int) -> Image.Image:
    page = doc.load_page(page_index)
    zoom = max(1/4, dpi / 72)  # safe lower bound
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

def _rotate_layer_to_canvas_size(base_rgb: Image.Image, overlay_rgba: Image.Image, angle45: bool) -> Image.Image:
    """Rotate overlay and paste it centered into a same-size transparent layer so alpha_composite always works."""
    if not angle45:
        # Already same size, just return
        return overlay_rgba
    rotated = overlay_rgba.rotate(45, expand=True)
    canvas = Image.new("RGBA", base_rgb.size, (0, 0, 0, 0))
    x = (canvas.width - rotated.width) // 2
    y = (canvas.height - rotated.height) // 2
    canvas.paste(rotated, (x, y), rotated)
    return canvas

def apply_watermark(img: Image.Image, wm: WatermarkSpec, tiled=True, rotate=False) -> Image.Image:
    """Draw watermark either tiled or single centered, with rotation step that preserves canvas size."""
    if not wm.text:
        return img

    # font size independent of DPI: % of page width
    font_px = max(4, int(img.width * (wm.size_pct / 100.0)))
    font_path = find_font()
    try:
        font = ImageFont.truetype(font_path, font_px) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    base_rgba = img.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    fill = (180, 180, 180, max(0, min(255, wm.opacity)))

    # Compute text metrics once
    tw, th = text_size(draw, wm.text, font)
    pad_px = int(font_px * (wm.tile_padding_pct / 100.0))
    step_x = max(1, tw + pad_px)
    step_y = max(1, th + pad_px)

    if tiled:
        # Cover beyond edges so rotation doesn't leave gaps
        start_x = -step_x
        start_y = -step_y
        end_x = img.width + step_x
        end_y = img.height + step_y

        # Stagger every other row slightly for nicer pattern
        offset = step_x // 2
        y = start_y
        row = 0
        while y < end_y:
            x_offset = offset if (row % 2 == 1) else 0
            x = start_x - x_offset
            while x < end_x:
                draw.text((x, y), wm.text, fill=fill, font=font)
                x += step_x
            y += step_y
            row += 1
    else:
        # Single centered (can be larger than page; it's okay to crop)
        pos = ((img.width - tw) / 2.0, (img.height - th) / 2.0)
        draw.text(pos, wm.text, fill=fill, font=font)

    # Rotate watermark layer and re-center to canvas size
    overlay = _rotate_layer_to_canvas_size(img, overlay, rotate)

    composed = Image.alpha_composite(base_rgba, overlay)
    return composed.convert("RGB")

def encode_image(img: Image.Image, fmt: str, quality: int) -> bytes:
    buf = io.BytesIO()
    if fmt.lower() == "jpeg":
        img.save(buf, format="JPEG", quality=max(1, min(100, quality)))
    else:
        img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def save_pdf(images: List[Image.Image], out_path: Path, quality: int):
    pdf = fitz.open()
    for img in images:
        data = encode_image(img, "jpeg", quality)
        rect = fitz.Rect(0, 0, img.width, img.height)
        page = pdf.new_page(width=img.width, height=img.height)
        page.insert_image(rect, stream=data)
    pdf.save(out_path)
    pdf.close()

def process_pdf(in_path: Path, out_dir: Path, opts: FlattenOptions, on_progress=None):
    outputs = []
    with fitz.open(in_path) as doc:
        total = doc.page_count
        images = []
        for i in range(total):
            img = render_page(doc, i, opts.dpi)
            img = apply_watermark(
                img,
                opts.watermark,
                tiled=opts.tiled,
                rotate=opts.rotate_45,
            )
            images.append(img)
            if on_progress:
                on_progress(i + 1, total)

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

    for im in images:
        try: im.close()
        except: pass
    return outputs

# ---------------- UI Helpers ----------------
def labeled_slider(title: str, slider: ft.Slider, unit: str = "", decimals: int = 0):
    """Wrap a Slider with a persistent label and live value text (no popup labels)."""
    slider.label = None  # we handle labels ourselves
    value_text = ft.Text(f"{round(slider.value, decimals)}{unit}", width=60, text_align=ft.TextAlign.END)
    def _on_change(e):
        value_text.value = f"{round(slider.value, decimals)}{unit}"
        slider.update()
        value_text.update()
    slider.on_change = _on_change
    row = ft.Row([ft.Text(title), ft.Container(expand=True), value_text], alignment=ft.MainAxisAlignment.START)
    col = ft.Column([row, slider], spacing=4)
    return col, slider

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
        self._preview_cache = {}  # (settings...) -> temp jpg path
        self._preview_debounce = None

        # Start temp cleanup thread
        threading.Thread(target=self._clean_temp_loop, daemon=True).start()

        # File pickers
        self.pick_files = ft.FilePicker(on_result=self._on_files)
        self.pick_folder = ft.FilePicker(on_result=self._on_folder)
        page.overlay.extend([self.pick_files, self.pick_folder])

        # Queued files
        self.files_list = ft.ListView(expand=True, height=160, spacing=6)
        self.add_btn = ft.ElevatedButton("Add PDFs…", on_click=self._pick_files)
        self.out_btn = ft.TextButton("Change output folder", on_click=self._pick_folder)
        self.output_label = ft.Text(f"Output: {self.output_dir}")

        # --- Controls (labels always visible)
        self.wm_text = ft.TextField(label="Watermark text", value="CONFIDENTIAL")

        self.wm_size_slider = ft.Slider(min=1, max=50, value=10, divisions=49)
        wm_size_row, self.wm_size_slider = labeled_slider("Watermark size %", self.wm_size_slider, unit="%", decimals=0)

        self.wm_opacity_slider = ft.Slider(min=30, max=255, value=120, divisions=225)
        wm_op_row, self.wm_opacity_slider = labeled_slider("Opacity", self.wm_opacity_slider, unit="", decimals=0)

        self.tile_padding_slider = ft.Slider(min=0, max=300, value=50, divisions=300)
        tile_pad_row, self.tile_padding_slider = labeled_slider("Tile padding % (of font size)", self.tile_padding_slider, unit="%", decimals=0)

        self.wm_tiled = ft.Checkbox(label="Tiled watermark", value=True, on_change=self._toggle_tile_controls)
        self.wm_angle = ft.Checkbox(label="Rotate 45°", value=False)

        self.dpi_slider = ft.Slider(min=72, max=600, value=150, divisions=528)
        dpi_row, self.dpi_slider = labeled_slider("DPI (rendering)", self.dpi_slider, unit="", decimals=0)

        self.quality_slider = ft.Slider(min=1, max=100, value=85, divisions=99)
        quality_row, self.quality_slider = labeled_slider("JPEG quality", self.quality_slider, unit="", decimals=0)

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
        self.preview_image = ft.Image(width=460, height=620, fit=ft.ImageFit.CONTAIN, visible=False)
        self.nav_controls = ft.Row([
            ft.IconButton(ft.Icons.ARROW_BACK, on_click=self._prev_page),
            ft.Text("Page", size=14),
            ft.IconButton(ft.Icons.ARROW_FORWARD, on_click=self._next_page),
        ], alignment=ft.MainAxisAlignment.CENTER)

        # Buttons
        self.process_btn = ft.FilledButton("Flatten & Export", on_click=self._process)
        self.clear_btn = ft.TextButton("Clear", on_click=self._clear)

        # Layout
        self.controls = [
            ft.Text("Lean PDF Flattener v4", size=20, weight=ft.FontWeight.BOLD),
            ft.Row([self.add_btn, self.out_btn]),
            self.output_label,
            ft.Text("Queued files:"), self.files_list,
            ft.Divider(),
            ft.Text("Watermark"),
            self.wm_text,
            wm_size_row,
            wm_op_row,
            tile_pad_row,
            ft.Row([self.wm_tiled, self.wm_angle]),
            ft.Divider(),
            ft.Text("Quality / Output"),
            dpi_row,
            quality_row,
            self.format,
            ft.Divider(),
            ft.Column([self.preview_image, self.nav_controls]),
            ft.Divider(),
            ft.Column([
                self.file_label, self.file_progress,
                self.overall_label, self.overall_progress,
                ft.Row([self.process_btn, self.clear_btn])
            ])
        ]

        # Hook preview updates (after label wrappers are created)
        for ctrl in [self.wm_text, self.wm_size_slider, self.wm_opacity_slider,
                     self.tile_padding_slider, self.wm_tiled, self.wm_angle,
                     self.dpi_slider]:
            prev = ctrl.on_change
            def make_handler(prev_handler):
                def handler(e):
                    if prev_handler: prev_handler(e)
                    self._debounced_update_preview()
                return handler
            ctrl.on_change = make_handler(prev)

    # ---------- Background cleanup ----------
    def _clean_temp_loop(self):
        tmpdir = Path(tempfile.gettempdir())
        while True:
            try:
                now = time.time()
                for f in tmpdir.glob("tmp*.jpg"):
                    if now - f.stat().st_mtime > 300:
                        f.unlink(missing_ok=True)
                time.sleep(300)
            except Exception:
                time.sleep(300)

    # ---------- File handling ----------
    def _pick_files(self, e): self.pick_files.pick_files(allow_multiple=True, allowed_extensions=["pdf"])
    def _on_files(self, e):
        if e.files:
            for f in e.files:
                path = Path(f.path)
                if path.exists() and path not in self.selected_files:
                    self.selected_files.append(path)
                    self.files_list.controls.append(ft.Text(str(path)))
            self.page.update()
            if self.selected_files:
                self._load_pdf_preview(self.selected_files[0])

    def _pick_folder(self, e): self.pick_folder.get_directory_path()
    def _on_folder(self, e):
        if e.path:
            self.output_dir = Path(e.path)
            self.output_label.value = f"Output: {self.output_dir}"
            self.page.update()

    def _clear(self, e=None):
        self.selected_files.clear()
        self.files_list.controls.clear()
        self.preview_image.visible = False
        if self._preview_doc:
            try: self._preview_doc.close()
            except: pass
        self._preview_doc = None
        self.page.update()

    # ---------- Processing ----------
    def _process(self, e):
        if not self.selected_files:
            self._show_message("No files selected")
            return
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        wm = WatermarkSpec(
            text=self.wm_text.value.strip(),
            size_pct=float(self.wm_size_slider.value),
            opacity=int(self.wm_opacity_slider.value),
            tile_padding_pct=float(self.tile_padding_slider.value),
        )
        opts = FlattenOptions(
            dpi=int(self.dpi_slider.value),
            jpeg_quality=int(self.quality_slider.value),
            export_format=self.format.value,
            watermark=wm,
            tiled=self.wm_tiled.value,
            rotate_45=self.wm_angle.value,
        )
        total = len(self.selected_files)
        self._show_progress(True)
        for i, file in enumerate(self.selected_files, start=1):
            self._file_label(f"Processing {file.name} ({i}/{total})")
            def per_page(cur, tot): self._file_prog(cur / max(1, tot))
            process_pdf(file, self.output_dir, opts, on_progress=per_page)
            self._overall_prog(i / total)
        self._done()

    # ---------- Preview ----------
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
            self._preview_cache.clear()
            self._update_preview_image()
        except Exception as ex:
            self._show_message(f"Failed to load PDF: {ex}")

    def _update_preview_image(self, e=None):
        try:
            if not self._preview_doc:
                return
            page_index = max(0, min(self._preview_page_index, len(self._preview_doc) - 1))

            cache_key = (
                page_index,
                self.wm_text.value,
                round(self.wm_size_slider.value, 3),
                int(self.wm_opacity_slider.value),
                round(self.tile_padding_slider.value, 3),
                bool(self.wm_tiled.value),
                bool(self.wm_angle.value),
                int(self.dpi_slider.value),
            )
            cached = self._preview_cache.get(cache_key)
            if cached and Path(cached).exists():
                self.preview_image.src = cached
                self.preview_image.visible = True
                self.page.update()
                return

            wm = WatermarkSpec(
                text=self.wm_text.value or "",
                size_pct=float(self.wm_size_slider.value or 10),
                opacity=int(self.wm_opacity_slider.value or 120),
                tile_padding_pct=float(self.tile_padding_slider.value or 50),
            )
            img = render_page(self._preview_doc, page_index, int(self.dpi_slider.value or 150))
            img = apply_watermark(
                img, wm,
                tiled=self.wm_tiled.value,
                rotate=self.wm_angle.value)

            # Downscale for UI if huge
            max_w = 1000
            if img.width > max_w:
                ratio = max_w / img.width
                img = img.resize((int(img.width * ratio), int(img.height * ratio)))

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            img.save(tmp.name, "JPEG", quality=70, optimize=True)
            img.close()

            self._preview_cache[cache_key] = tmp.name
            self.preview_image.src = tmp.name
            self.preview_image.visible = True
            self.page.update()
        except Exception as ex:
            log.exception("Preview failed")
            self._show_message(f"Preview failed: {ex}")

    def _debounced_update_preview(self, e=None):
        try:
            if self._preview_debounce:
                self._preview_debounce.cancel()
        except Exception:
            pass
        def run(): self._update_preview_image()
        self._preview_debounce = threading.Timer(0.20, run)
        self._preview_debounce.start()

    def _next_page(self, e):
        if self._preview_doc and self._preview_page_index < len(self._preview_doc) - 1:
            self._preview_page_index += 1
            self._update_preview_image()

    def _prev_page(self, e):
        if self._preview_doc and self._preview_page_index > 0:
            self._preview_page_index -= 1
            self._update_preview_image()

    def _toggle_tile_controls(self, e=None):
        # No hidden controls here, but we can trigger a re-preview immediately
        self._debounced_update_preview()

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

    def _show_message(self, text):
        self.page.snack_bar = ft.SnackBar(ft.Text(text))
        self.page.snack_bar.open = True
        self.page.update()

# ---------------- Entry Point ----------------
def main(page: ft.Page):
    page.title = "Lean PDF Flattener v4"
    page.window_width = 1000
    page.window_height = 820
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 16
    page.scroll = "adaptive"
    app = PDFToolApp(page)
    page.add(app)

if __name__ == "__main__":
    ft.app(target=main)

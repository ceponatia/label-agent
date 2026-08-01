from pathlib import Path


def labels_root(data_dir: str | Path) -> Path:
    return Path(data_dir) / "labels"


def label_dir(data_dir: str | Path, label_id: int) -> Path:
    path = labels_root(data_dir) / str(label_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def original_pdf_path(data_dir: str | Path, label_id: int) -> Path:
    return label_dir(data_dir, label_id) / "original.pdf"


def print_pdf_path(data_dir: str | Path, label_id: int) -> Path:
    return label_dir(data_dir, label_id) / "print.pdf"


def preview_png_path(data_dir: str | Path, label_id: int, which: str = "print") -> Path:
    return label_dir(data_dir, label_id) / f"{which}-preview.png"

from pathlib import Path


def labels_root(data_dir: str | Path) -> Path:
    return Path(data_dir) / "labels"


def label_dir(data_dir: str | Path, label_id: int) -> Path:
    """Where a label's files live. Naming the path never creates it.

    The read paths ask for these paths too, and a directory conjured up by a
    look is not free: viewing a pruned label would leave an empty directory
    behind for pruning to find and report as pruned all over again.
    """
    return labels_root(data_dir) / str(label_id)


def ensure_label_dir(data_dir: str | Path, label_id: int) -> Path:
    """`label_dir`, created if missing. For the paths that are about to write."""
    path = label_dir(data_dir, label_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def original_pdf_path(data_dir: str | Path, label_id: int) -> Path:
    return label_dir(data_dir, label_id) / "original.pdf"


def print_pdf_path(data_dir: str | Path, label_id: int) -> Path:
    return label_dir(data_dir, label_id) / "print.pdf"


def preview_png_path(data_dir: str | Path, label_id: int, which: str = "print") -> Path:
    return label_dir(data_dir, label_id) / f"{which}-preview.png"

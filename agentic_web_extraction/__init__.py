from .cli import app
from .extractor import Extractor
from .result import ExtractionResult, ScreenVerdict, Usage

__all__ = ["Extractor", "ExtractionResult", "ScreenVerdict", "Usage", "main"]


def main() -> None:
    app()

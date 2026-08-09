"""Permite `python -m flood_validation ...` además de
`python -m flood_validation.main ...`. Correr desde src/."""

from flood_validation.main import main

if __name__ == "__main__":
    main()

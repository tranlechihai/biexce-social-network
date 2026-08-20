"""Entry point for ``python -m ting_ting``.

Usage:
    python -m ting_ting            — print config summary
    python -m ting_ting seed       — run the guarded seed
"""

import sys

from ting_ting.main import main


def _cli():
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        from ting_ting.seed import main as seed_main
        seed_main()
    else:
        main()


if __name__ == "__main__":
    _cli()

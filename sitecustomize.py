# SPDX-License-Identifier: GPL-3.0-only
# Store subprocesses acquire selected-file grants before importing render code.
import os
if os.environ.get('LIGHTTABLE_STORE_GRANTS_FILE'):
    import atexit
    try:
        import macos_store_access
        macos_store_access.refresh()
    except Exception as error:
        # Python normally ignores sitecustomize errors; Store workers must not.
        import sys
        print(f"LightTable file access needs reauthorization: {type(error).__name__}: {error}", file=sys.stderr)
        os._exit(78)
    atexit.register(macos_store_access.close)

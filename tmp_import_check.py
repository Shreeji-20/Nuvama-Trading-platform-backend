import importlib.util, sys, pathlib, traceback

p = pathlib.Path(r'c:/Users/shree/OneDrive/Desktop/TrueData/nuvama/stratergies.py')
try:
    # ensure the directory containing order_class.py is on sys.path so
    # "from order_class import Orders" resolves when loading the module
    sys.path.insert(0, str(p.parent))
    spec = importlib.util.spec_from_file_location('nuvama.stratergies', p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    print('IMPORT_OK')
except Exception:
    traceback.print_exc()
    sys.exit(1)

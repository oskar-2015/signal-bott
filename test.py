# -*- coding: utf-8 -*-
import sys
print("=" * 60, flush=True)
print("Starting diagnostic test...", flush=True)
print(f"Python: {sys.version}", flush=True)
print("=" * 60, flush=True)

# اختبار 1
try:
    print("[1/5] Testing database.py...", flush=True)
    import database
    print("✅ database.py imported OK", flush=True)
except Exception as e:
    print(f"❌ database.py FAILED: {type(e).__name__}: {e}", flush=True)
    raise

# اختبار 2
try:
    print("[2/5] Testing quotex_manager.py...", flush=True)
    import quotex_manager
    print("✅ quotex_manager.py imported OK", flush=True)
except Exception as e:
    print(f"❌ quotex_manager.py FAILED: {type(e).__name__}: {e}", flush=True)
    raise

# اختبار 3
try:
    print("[3/5] Testing assets.py...", flush=True)
    import assets
    print("✅ assets.py imported OK", flush=True)
except Exception as e:
    print(f"❌ assets.py FAILED: {type(e).__name__}: {e}", flush=True)
    raise

# اختبار 4
try:
    print("[4/5] Testing license_manager.py...", flush=True)
    import license_manager
    print("✅ license_manager.py imported OK", flush=True)
except Exception as e:
    print(f"❌ license_manager.py FAILED: {type(e).__name__}: {e}", flush=True)
    raise

# اختبار 5
try:
    print("[5/5] Testing app.py...", flush=True)
    import app
    print("✅ app.py imported OK", flush=True)
except Exception as e:
    print(f"❌ app.py FAILED: {type(e).__name__}: {e}", flush=True)
    raise

print("=" * 60, flush=True)
print("🎉 ALL MODULES LOADED SUCCESSFULLY", flush=True)
print("=" * 60, flush=True)

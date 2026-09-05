import os
import shutil
import zipfile
from pathlib import Path

def create_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "__init__.py").touch(exist_ok=True)

def main():
    root = Path(r"D:\BetDoc")
    zip_path = Path(r"C:\Users\Amit Patnaik\Downloads\betting-quant-platform.zip")
    temp_extract = root / "temp_extract"
    
    print("1. Extracting original files from Downloads...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(temp_extract)
        
    # The zip contains a root folder "betting-quant-platform" inside it usually, or just the files.
    # Let's locate the math_engine dynamically.
    math_engine_path = None
    for path in temp_extract.rglob("math_engine"):
        if path.is_dir():
            math_engine_path = path
            break
            
    if not math_engine_path:
        print("[-] Could not find math_engine in zip. Aborting.")
        return

    print("2. Rebuilding the Hexagonal Vault (src/betdoc)...")
    src_betdoc = root / "src" / "betdoc"
    domain = src_betdoc / "domain"
    
    create_dir(domain / "models")
    create_dir(domain / "pricing")
    create_dir(domain / "arbitrage")
    create_dir(domain / "staking")
    create_dir(domain / "parlay")
    create_dir(domain / "risk")
    
    application = src_betdoc / "application"
    create_dir(application / "ports")
    create_dir(application / "services")
    
    adapters = src_betdoc / "adapters"
    create_dir(adapters / "bookmakers")
    create_dir(adapters / "persistence")
    create_dir(adapters / "cache")
    create_dir(adapters / "notifiers")
    
    services = src_betdoc / "services"
    create_dir(services / "ingestor")
    create_dir(services / "book")
    create_dir(services / "scanner")
    create_dir(services / "executor")
    create_dir(services / "api")

    print("3. Moving Claude Sonnet's Math into the Vault...")
    if (math_engine_path / "devig.py").exists():
        shutil.copy(math_engine_path / "devig.py", domain / "pricing" / "devig.py")
        shutil.copy(math_engine_path / "arbitrage.py", domain / "arbitrage" / "arbitrage.py")
        shutil.copy(math_engine_path / "kelly.py", domain / "staking" / "kelly.py")
        shutil.copy(math_engine_path / "portfolio_kelly.py", domain / "staking" / "portfolio_kelly.py")
        shutil.copy(math_engine_path / "parlay.py", domain / "parlay" / "parlay.py")
        shutil.copy(math_engine_path / "clv.py", domain / "risk" / "clv.py")

    print("4. Cleaning up temp files...")
    shutil.rmtree(temp_extract)
    
    cleanup_script = root / "cleanup.py"
    if cleanup_script.exists():
        os.remove(cleanup_script)
        
    print("==================================================")
    print("RECOVERY COMPLETE. The 'src' folder is perfectly restored.")
    print("Source Control should jump back up now!")

if __name__ == "__main__":
    main()

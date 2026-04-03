from pathlib import Path
import shutil

# デスクトップのパス
desktop = Path.home() / "Desktop"

# 仕分けルール
rules = {
    ".dmg": "Installers",
    ".pkg": "Installers",
    ".py": "Python",
    ".jpg": "Images",
    ".png": "Images",
    ".txt": "Text",
    ".pdf": "PDF"
}

for file in desktop.iterdir():
    if file.is_file():
        ext = file.suffix.lower()

        if ext in rules:
            folder = desktop / rules[ext]
            folder.mkdir(exist_ok=True)

            shutil.move(str(file), str(folder / file.name))
            print(f"Moved: {file.name} → {folder}")


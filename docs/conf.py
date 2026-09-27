import os
import sys

# backend документируется как пакет app, поэтому его папка идёт первой.
# ml_service нужен только ради runtime, у него тоже есть пакет app, поэтому он последний.
root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
for sub in ('ml_service', 'map_matching/src', 'ndtp-parser', 'backend'):
    sys.path.insert(0, os.path.join(root, sub))

project = 'Московский Транспорт: Хакатон 2026'
copyright = '2026, Team'
author = 'Team'

extensions = [
    'sphinx.ext.autodoc',       # Вытягивает docstrings из кода
    'sphinx.ext.napoleon',      # Поддержка Google/NumPy стиля docstrings
    'sphinx.ext.viewcode',      # Добавляет ссылки на исходный код
    'myst_parser',              # Поддержка Markdown (.md)
]

# Игнорируем ошибки (удалено)

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

# Тема оформления
html_theme = 'sphinx_rtd_theme'
html_static_path = []

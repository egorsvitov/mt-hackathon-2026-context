import os
import sys

# Добавляем пути к нашему коду, чтобы Sphinx мог его найти
basedir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend'))
sys.path.insert(0, basedir)
sys.path.insert(0, os.path.join(basedir, 'app'))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'ml_service')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'ml_service', 'app')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'ndtp-parser')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'map_matching', 'src')))

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
html_static_path = ['_static']

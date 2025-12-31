# Copyright (c) 2022 The ARA Records Ansible authors
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import os
import sys

from sphinx_pyproject import SphinxConfig

from ara.setup import _version

# -- General configuration ----------------------------------------------------
# Most of the general configuration should be handled in pyproject.toml

config = SphinxConfig(
    "../../pyproject.toml",
    globalns=globals(),
    config_overrides={
        "version": ".".join(map(str, _version.version_tuple[:3])),  # The short X.Y.Z version.
        "release": _version.version,  # The full version, including alpha/beta/rc tags.
    },
)

sys.path.insert(0, os.path.abspath("../.."))

# -- Options for HTML output --------------------------------------------------

# The theme to use for HTML and HTML Help pages.  Major themes that come with
# Sphinx are currently 'default' and 'sphinxdoc'.
html_theme = config.get("html_theme", "sphinx_rtd_theme")
html_theme_path = []
html_static_path = ["_static"]

# Output file base name for HTML help builder.
htmlhelp_basename = "%sdoc" % project

# Grouping the document tree into LaTeX files. List of tuples
# (source start file, target name, title, author, documentclass
# [howto/manual]).
latex_documents = [
    ("index", "%s.tex" % project, "%s Documentation" % project, "ARA Records Ansible authors", "manual"),
]

# Example configuration for intersphinx: refer to the Python standard library.
# intersphinx_mapping = {'http://docs.python.org/': None}

# -*- coding: utf-8 -*-

from __future__ import unicode_literals

try:
    from collections.abc import Sequence, Mapping
    str_type = str
except ImportError:  # python 2
    from collections import Sequence, Mapping
    str_type = basestring
import copy
import io
import itertools
import os
import re

import github
import ruamel.yaml

from conda_build.metadata import (ensure_valid_license_family,
                                  FIELDS as cbfields)
import conda_build.conda_interface

from .utils import render_meta_yaml


FIELDS = copy.deepcopy(cbfields)

# Just in case 'extra' moves into conda_build
if 'extra' not in FIELDS.keys():
    FIELDS['extra'] = set()

FIELDS['extra'].add('recipe-maintainers')

EXPECTED_SECTION_ORDER = ['package', 'source', 'build', 'requirements',
                          'test', 'app', 'outputs', 'about', 'extra']

REQUIREMENTS_ORDER = ['build', 'host', 'run']

TEST_KEYS = {'imports', 'commands'}
TEST_FILES = ['run_test.py', 'run_test.sh', 'run_test.bat', 'run_test.pl']

sel_pat = re.compile(r'(.+?)\s*(#.*)?\[([^\[\]]+)\](?(2).*)$')
jinja_pat = re.compile(r'\s*\{%\s*(set)\s+[^\s]+\s*=\s*[^\s]+\s*%\}')


def get_section(parent, name, lints):
    if name == 'source':
        return get_list_section(parent, name, lints, allow_single=True)
    elif name == 'outputs':
        return get_list_section(parent, name, lints)

    section = parent.get(name, {})
    if not isinstance(section, Mapping):
        lints.append('The "{}" section was expected to be a dictionary, but '
                     'got a {}.'.format(name, type(section).__name__))
        section = {}
    return section


def get_list_section(parent, name, lints, allow_single=False):
    section = parent.get(name, [])
    if allow_single and isinstance(section, Mapping):
        return [section]
    elif isinstance(section, Sequence) and not isinstance(section, str_type):
        return section
    else:
        msg = ('The "{}" section was expected to be a {}list, but got a {}.{}.'
               .format(name, "dictionary or a " if allow_single else "",
                       type(section).__module__, type(section).__name__))
        lints.append(msg)
        return [{}]


def lint_section_order(major_sections, lints):
    section_order_sorted = sorted(major_sections,
                                  key=EXPECTED_SECTION_ORDER.index)
    if major_sections != section_order_sorted:
        section_order_sorted_str = map(lambda s: "'%s'" % s,
                                       section_order_sorted)
        section_order_sorted_str = ", ".join(section_order_sorted_str)
        section_order_sorted_str = "[" + section_order_sorted_str + "]"
        lints.append('The top level meta keys are in an unexpected order. '
                     'Expecting {}.'.format(section_order_sorted_str))


def lint_about_contents(about_section, lints):
    for about_item in ['home', 'license', 'summary']:
        # if the section doesn't exist, or is just empty, lint it.
        if not about_section.get(about_item, ''):
            lints.append('The {} item is expected in the about section.'
                         ''.format(about_item))


def lintify(meta, recipe_dir=None, conda_forge=False):
    lints = []
    hints = []
    major_sections = list(meta.keys())

    # If the recipe_dir exists (no guarantee within this function) , we can
    # find the meta.yaml within it.
    meta_fname = os.path.join(recipe_dir or '', 'meta.yaml')

    sources_section = get_section(meta, 'source', lints)
    build_section = get_section(meta, 'build', lints)
    requirements_section = get_section(meta, 'requirements', lints)
    test_section = get_section(meta, 'test', lints)
    about_section = get_section(meta, 'about', lints)
    extra_section = get_section(meta, 'extra', lints)
    package_section = get_section(meta, 'package', lints)
    outputs_section = get_section(meta, 'outputs', lints)

    # 0: Top level keys should be expected
    unexpected_sections = []
    for section in major_sections:
        if section not in EXPECTED_SECTION_ORDER:
            lints.append('The top level meta key {} is unexpected' .format(section))
            unexpected_sections.append(section)

    for section in unexpected_sections:
        major_sections.remove(section)

    # 1: Top level meta.yaml keys should have a specific order.
    lint_section_order(major_sections, lints)

    # 2: The about section should have a home, license and summary.
    lint_about_contents(about_section, lints)

    # 3a: The recipe should have some maintainers.
    if not extra_section.get('recipe-maintainers', []):
        lints.append('The recipe could do with some maintainers listed in '
                     'the `extra/recipe-maintainers` section.')

    # 3b: Maintainers should be a list
    if not (isinstance(extra_section.get('recipe-maintainers', []), Sequence)
            and not isinstance(extra_section.get('recipe-maintainers', []),
                               str_type)):
        lints.append('Recipe maintainers should be a json list.')

    # 4: The recipe should have some tests.
    if not any(key in TEST_KEYS for key in test_section):
        a_test_file_exists = (recipe_dir is not None and
                              any(os.path.exists(os.path.join(recipe_dir,
                                                              test_file))
                                  for test_file in TEST_FILES))
        if not a_test_file_exists:
            has_outputs_test = False
            no_test_hints = []
            if outputs_section:
                for out in outputs_section:
                    test_out = get_section(out, 'test', lints)
                    if any(key in TEST_KEYS for key in test_out):
                        has_outputs_test = True
                    else:
                        no_test_hints.append(
                            "It looks like the '{}' output doesn't "
                            "have any tests.".format(out.get('name', '???')))

            if has_outputs_test:
                hints.extend(no_test_hints)
            else:
                lints.append('The recipe must have some tests.')

    # 5: License cannot be 'unknown.'
    license = about_section.get('license', '').lower()
    if 'unknown' == license.strip():
        lints.append('The recipe license cannot be unknown.')

    # 6: Selectors should be in a tidy form.
    if recipe_dir is not None and os.path.exists(meta_fname):
        bad_selectors = []
        bad_lines = []
        # Good selectors look like ".*\s\s#\s[...]"
        good_selectors_pat = re.compile(r'(.+?)\s{2,}#\s\[(.+)\](?(2).*)$')
        with io.open(meta_fname, 'rt') as fh:
            for selector_line, line_number in selector_lines(fh):
                if not good_selectors_pat.match(selector_line):
                    bad_selectors.append(selector_line)
                    bad_lines.append(line_number)
        if bad_selectors:
            lints.append('Selectors are suggested to take a '
                         '``<two spaces>#<one space>[<expression>]`` form.'
                         ' See lines {}'.format(bad_lines))

    # 7: The build section should have a build number.
    if build_section.get('number', None) is None:
        lints.append('The recipe must have a `build/number` section.')

    # 8: The build section should be before the run section in requirements.
    seen_requirements = [
            k for k in requirements_section if k in REQUIREMENTS_ORDER]
    requirements_order_sorted = sorted(seen_requirements,
                                       key=REQUIREMENTS_ORDER.index)
    if seen_requirements != requirements_order_sorted:
        lints.append('The `requirements/` sections should be defined '
                     'in the following order: ' + ', '.join(REQUIREMENTS_ORDER)
                     + '; instead saw: ' + ', '.join(seen_requirements) + '.')

    # 9: Files downloaded should have a hash.
    for source_section in sources_section:
        if ('url' in source_section and
                not ({'sha1', 'sha256', 'md5'} & set(source_section.keys()))):
            lints.append('When defining a source/url please add a sha256, sha1 '
                         'or md5 checksum (sha256 preferably).')

    # 10: License should not include the word 'license'.
    license = about_section.get('license', '').lower()
    if 'license' in license.lower():
        lints.append('The recipe `license` should not include the word '
                     '"License".')

    # 11: There should be one empty line at the end of the file.
    if recipe_dir is not None and os.path.exists(meta_fname):
        with io.open(meta_fname, 'r') as f:
            lines = f.read().split('\n')
        # Count the number of empty lines from the end of the file
        empty_lines = itertools.takewhile(lambda x: x == '', reversed(lines))
        end_empty_lines_count = len(list(empty_lines))
        if end_empty_lines_count > 1:
            lints.append('There are {} too many lines.  '
                         'There should be one empty line at the end of the '
                         'file.'.format(end_empty_lines_count - 1))
        elif end_empty_lines_count < 1:
            lints.append('There are too few lines.  There should be one empty '
                         'line at the end of the file.')

    # 12: License family must be valid (conda-build checks for that)
    try:
        ensure_valid_license_family(meta)
    except RuntimeError as e:
        lints.append(str(e))

    # 13: Check that the recipe name is valid
    recipe_name = package_section.get('name', '').strip()
    if re.match('^[a-z0-9_\-.]+$', recipe_name) is None:
        lints.append('Recipe name has invalid characters. only lowercase alpha, numeric, '
                     'underscores, hyphens and dots allowed')

    # 14: Run conda-forge specific lints
    if conda_forge:
        run_conda_forge_specific(meta, recipe_dir, lints, hints)

    # 15: Check if we are using legacy patterns
    build_reqs = requirements_section.get('build', None)
    if build_reqs and ('numpy x.x' in build_reqs):
        lints.append('Using pinned numpy packages is a deprecated pattern.  Consider '
                     'using the method outlined '
                     '[here](https://conda-forge.org/docs/meta.html#building-against-numpy).')

    # 16: Subheaders should be in the allowed subheadings
    for section in major_sections:
        expected_subsections = FIELDS.get(section, [])
        if not expected_subsections:
            continue
        for subsection in get_section(meta, section, lints):
            if (section != 'source'
                and section != 'outputs'
                and subsection not in expected_subsections):
                lints.append('The {} section contained an unexpected '
                             'subsection name. {} is not a valid subsection'
                             ' name.'.format(section, subsection))
            elif section == 'source' or section == 'outputs':
                for source_subsection in subsection:
                    if source_subsection not in expected_subsections:
                        lints.append('The {} section contained an unexpected '
                                     'subsection name. {} is not a valid subsection'
                                     ' name.'.format(section, source_subsection))


    # 17: noarch doesn't work with selectors
    if build_section.get('noarch') is not None and os.path.exists(meta_fname):
        with io.open(meta_fname, 'rt') as fh:
            in_requirements = False
            for line in fh:
                line_s = line.strip()
                if (line_s == "requirements:"):
                    in_requirements = True
                    requirements_spacing = line[:-len(line.lstrip())]
                    continue
                if line_s.startswith("skip:") and is_selector_line(line):
                    lints.append("`noarch` packages can't have selectors. If "
                                 "the selectors are necessary, please remove "
                                 "`noarch: {}`.".format(build_section['noarch']))
                    break
                if in_requirements:
                    if requirements_spacing == line[:-len(line.lstrip())]:
                        in_requirements = False
                        continue
                    if is_selector_line(line):
                        lints.append("`noarch` packages can't have selectors. If "
                                     "the selectors are necessary, please remove "
                                     "`noarch: {}`.".format(build_section['noarch']))
                        break

    # 19: check version
    if package_section.get('version') is not None:
        ver = str(package_section.get('version'))
        try:
            conda_build.conda_interface.VersionOrder(ver)
        except:
            lints.append("Package version {} doesn't match conda spec".format(ver))

    # 20: Jinja2 variable definitions should be nice.
    if recipe_dir is not None and os.path.exists(meta_fname):
        bad_jinja = []
        bad_lines = []
        # Good Jinja2 variable definitions look like "{% set .+ = .+ %}"
        good_jinja_pat = re.compile(r'\s*\{%\s(set)\s[^\s]+\s=\s[^\s]+\s%\}')
        with io.open(meta_fname, 'rt') as fh:
            for jinja_line, line_number in jinja_lines(fh):
                if not good_jinja_pat.match(jinja_line):
                    bad_jinja.append(jinja_line)
                    bad_lines.append(line_number)
        if bad_jinja:
            lints.append('Jinja2 variable definitions are suggested to '
                         'take a ``{{%<one space>set<one space>'
                         '<variable name><one space>=<one space>'
                         '<expression><one space>%}}`` form. See lines '
                         '{}'.format(bad_lines))
    
    # 21: Legacy usage of compilers
    if build_reqs and ('toolchain' in build_reqs):
        lints.append('Using toolchain directly in this manner is deprecated.  Consider '
                     'using the compilers outlined '
                     '[here](https://conda-forge.org/docs/meta.html#compilers).')

    # hints
    # 1: suggest pip
    if 'script' in build_section:
        scripts = build_section['script']
        if isinstance(scripts, str):
            scripts = [scripts]
        for script in scripts:
            if 'python setup.py install' in script:
                hints.append('Whenever possible python packages should use pip. '
                             'See https://conda-forge.org/docs/meta.html#use-pip')

    return lints, hints


def run_conda_forge_specific(meta, recipe_dir, lints, hints):
    gh = github.Github(os.environ['GH_TOKEN'])
    package_section = get_section(meta, 'package', lints)
    extra_section = get_section(meta, 'extra', lints)
    recipe_dirname = os.path.basename(recipe_dir) if recipe_dir else 'recipe'
    recipe_name = package_section.get('name', '').strip()
    is_staged_recipes = recipe_dirname != 'recipe'

    # 1: Check that the recipe does not exist in conda-forge or bioconda
    if is_staged_recipes and recipe_name:
        cf = gh.get_user(os.getenv('GH_ORG', 'conda-forge'))
        try:
            cf.get_repo('{}-feedstock'.format(recipe_name))
            feedstock_exists = True
        except github.UnknownObjectException as e:
            feedstock_exists = False

        if feedstock_exists:
            lints.append('Feedstock with the same name exists in conda-forge')

        bio = gh.get_user('bioconda').get_repo('bioconda-recipes')
        try:
            bio.get_dir_contents('recipes/{}'.format(recipe_name))
        except github.UnknownObjectException as e:
            pass
        else:
            hints.append('Recipe with the same name exists in bioconda: '
                         'please discuss with @conda-forge/bioconda-recipes.')

    # 2: Check that the recipe maintainers exists:
    maintainers = extra_section.get('recipe-maintainers', [])
    for maintainer in maintainers:
        if "/" in maintainer:
            # It's a team. Checking for existence is expensive. Skip for now
            continue
        try:
            gh.get_user(maintainer)
        except github.UnknownObjectException as e:
            lints.append('Recipe maintainer "{}" does not exist'.format(maintainer))

    # 3: if the recipe dir is inside the example dir
    if recipe_dir is not None and 'recipes/example/' in recipe_dir:
        lints.append('Please move the recipe out of the example dir and '
                     'into its own dir.')


def is_selector_line(line):
    # Using the same pattern defined in conda-build (metadata.py),
    # we identify selectors.
    line = line.rstrip()
    if line.lstrip().startswith('#'):
        # Don't bother with comment only lines
        return False
    m = sel_pat.match(line)
    if m:
        m.group(3)
        return True
    return False


def is_jinja_line(line):
    line = line.rstrip()
    m = jinja_pat.match(line)
    if m:
        return True
    return False


def selector_lines(lines):
    for i, line in enumerate(lines):
        if is_selector_line(line):
            yield line, i


def jinja_lines(lines):
    for i, line in enumerate(lines):
        if is_jinja_line(line):
            yield line, i


def main(recipe_dir, conda_forge=False, return_hints=False):
    recipe_dir = os.path.abspath(recipe_dir)
    recipe_meta = os.path.join(recipe_dir, 'meta.yaml')
    if not os.path.exists(recipe_dir):
        raise IOError('Feedstock has no recipe/meta.yaml.')

    with io.open(recipe_meta, 'rt') as fh:
        content = render_meta_yaml(''.join(fh))
        meta = ruamel.yaml.load(content, ruamel.yaml.RoundTripLoader)
    results, hints = lintify(meta, recipe_dir, conda_forge)
    if return_hints:
        return results, hints
    else:
        return results

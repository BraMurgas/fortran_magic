# -*- coding: utf-8 -*-
"""
=====================
Fortran 90/f2py magic
=====================

{FORTRAN_DOC}



Author:
* Martín Gaitán <gaitan@gmail.com>
"""

from __future__ import print_function

import imp
import io
import os
import sys

try:
    import hashlib
except ImportError:
    import md5 as hashlib

from IPython.core.error import UsageError
from IPython.core.magic import Magics, magics_class, line_magic, cell_magic
from IPython.core import display, magic_arguments
from IPython.utils import py3compat
from IPython.utils.io import capture_output
from IPython.utils.path import get_ipython_cache_dir
from IPython.config.configurable import Configurable
from IPython.utils.traitlets import Int, Float, Unicode, Bool
from numpy.f2py import f2py2e
from numpy.distutils import fcompiler
from distutils.core import Distribution
from distutils.ccompiler import compiler_class
from distutils.command.build_ext import build_ext

__version__ = '0.2.1'
fcompiler.load_all_fcompiler_classes()


def compose(*decors):
    """Helper to compose decorators::

        @a
        @b
        @c
        def f():
            pass

    Would be equivalent to::

        @compose(a, b, c)
        def f():
            ...
    """
    def composed(f):
        for decor in reversed(decors):
            f = decor(f)
        return f
    return composed


class FortranConfig(Configurable):
    verbosity = Int(0, config=True)
    fcompiler = Unicode('', allow_none=True, config=True)


@magics_class
class FortranMagics(Magics, FortranConfig):

    allowed_fcompilers = sorted(fcompiler.fcompiler_class.keys())
    allowed_compilers = sorted(compiler_class.keys())

    arguments = compose(
        magic_arguments.magic_arguments(),
        magic_arguments.argument(
            "-v", "--verbosity", action="count", default=0,
            help="increase output verbosity"
        ),
        magic_arguments.argument(
            '--fcompiler',
            choices=allowed_fcompilers,
            help="""Specify Fortran compiler type by vendor.
                 See %%f2py_help --fcompiler""",
        ),
        magic_arguments.argument(
            '--compiler',
            choices=allowed_compilers,
            help="""Specify C compiler type (as defined by distutils).
                    See %%f2py_help --compiler"""
        ),
        magic_arguments.argument(
            '--f90flags', help="Specify F90 compiler flags"
        ),
        magic_arguments.argument(
            '--f77flags', help="Specify F77 compiler flags"
        ),
        magic_arguments.argument(
            '--opt', help="Specify optimization flags"
        ),
        magic_arguments.argument(
            '--arch', help="Specify architecture specific optimization flags"
        ),
        magic_arguments.argument(
            '--noopt', action="store_true", help="Compile without optimization"
        ),
        magic_arguments.argument(
            '--noarch', action="store_true", help="Compile without "
            "arch-dependent optimization"
        ),
        magic_arguments.argument(
            '--debug', action="store_true", help="Compile with debugging "
            "information"
        ),
        magic_arguments.argument(
            '--link', action='append', default=[],
            help="""Link extension module with LINK resource, as defined
                    by numpy.distutils/system_info.py. E.g. to link
                    with optimized LAPACK libraries (vecLib on MacOSX,
                    ATLAS elsewhere), use --link lapack_opt.
                    See also %%f2py_help --resources switch."""
        ))

    def __init__(self, shell):
        super(FortranMagics, self).__init__(shell=shell)
        super(FortranConfig, self).__init__(config=shell.config)
        self.shell.configurables.append(self)
        self._reloads = {}
        self._code_cache = {}
        self._lib_dir = os.path.join(get_ipython_cache_dir(), 'fortran')
        if not os.path.exists(self._lib_dir):
            os.makedirs(self._lib_dir)

    def _import_all(self, module, verbosity=0):
        imported = []
        for k, v in module.__dict__.items():
            if not k.startswith('__'):
                self.shell.push({k: v})
                imported.append(k)
        if verbosity > 0 and imported:
            print("\nOk. The following fortran objects "
                  "are ready to use: %s" % ", ".join(imported))

    def _run_f2py(self, argv, show_captured=False, verbosity=0):
        """
        Here we directly call the numpy.f2py.f2py2e.run_compile() entry point,
        after some small amount of setup to get sys.argv and the current
        working directory set appropriately.
        """
        old_argv = sys.argv
        old_cwd = os.getcwdu() if sys.version_info[0] == 2 else os.getcwd()
        try:
            sys.argv = ['f2py'] + list(map(str, argv))
            if verbosity > 1:
                print("Running...\n   %s" % ' '.join(sys.argv))

            os.chdir(self._lib_dir)
            try:
                with capture_output() as captured:
                    f2py2e.main()
                if show_captured or verbosity > 2:
                    captured()
            except SystemExit as e:
                captured()
                raise UsageError(str(e))
        finally:
            sys.argv = old_argv
            os.chdir(old_cwd)

    @magic_arguments.magic_arguments()
    @magic_arguments.argument(
        '--resources', action="store_true",
        help="""List system resources found by system_info.py.

                See also
                %%f2py_help --link <resource> switch.
                """
    )
    @magic_arguments.argument(
        '--link',
        help="""Given a resource name, show what it foun.
                E.g. try '--link lapack.

                See also
                %%f2py_help --link <resource> switch.
                """
    )
    @magic_arguments.argument(
        '--fcompiler', action="store_true",
        help="List available Fortran compilers",
    )
    @magic_arguments.argument(
        '--compiler', action="store_true",
        help="List available C compilers",
    )
    @line_magic
    def f2py_help(self, line):
        args = magic_arguments.parse_argstring(self.f2py_help, line)
        if args.fcompiler:
            self._run_f2py(['-c', '--help-fcompiler'], True)
        elif args.compiler:
            self._run_f2py(['-c', '--help-compiler'], True)
        elif args.resources:
            self._run_f2py(['--help-link'], True)
        elif args.link:
            self._run_f2py(['--help-link', args.link], True)

    @arguments
    @cell_magic
    def fortran(self, line, cell):
        """Compile and import everything from a Fortran code cell, using f2py.

        The content of the cell is written to a `.f90` file in the
        directory `IPYTHONDIR/fortran` using a filename with the hash of the
        code. This file is then compiled. The resulting module
        is imported and all of its symbols are injected into the user's
        namespace.


        Usage
        =====
        Prepend ``%%fortran`` to your fortran code in a cell::

        ``%%fortran

        ! put your code here.
        ``


        """
        args = magic_arguments.parse_argstring(self.fortran, line)

        # boolean flags
        f2py_args = ['--%s' % k for k, v in vars(args).items() if v is True]

        kw = ['--%s=%s' % (k, v) for k, v in vars(args).items()
              if isinstance(v, basestring)]
        f2py_args.extend(kw)

        # link resoucers
        if args.link:
            resources = ['--link-%s' % r for r in args.link]
            f2py_args.extend(resources)

        code = cell if cell.endswith('\n') else cell+'\n'
        key = code, sys.version_info, sys.executable, f2py2e.f2py_version

        module_name = "_fortran_magic_" + \
                      hashlib.md5(str(key).encode('utf-8')).hexdigest()

        module_path = os.path.join(self._lib_dir, module_name + self.so_ext)

        f90_file = os.path.join(self._lib_dir, module_name + '.f90')
        f90_file = py3compat.cast_bytes_py2(f90_file,
                                            encoding=sys.getfilesystemencoding())
        with io.open(f90_file, 'w', encoding='utf-8') as f:
            f.write(code)

        self._run_f2py(f2py_args + ['-m', module_name, '-c', f90_file],
                       verbosity=args.verbosity)

        self._code_cache[key] = module_name
        module = imp.load_dynamic(module_name, module_path)
        self._import_all(module, verbosity=args.verbosity)

    @property
    def so_ext(self):
        """The extension suffix for compiled modules."""
        try:
            return self._so_ext
        except AttributeError:

            dist = Distribution()
            config_files = dist.find_config_files()
            try:
                config_files.remove('setup.cfg')
            except ValueError:
                pass
            dist.parse_config_files(config_files)
            build_extension = build_ext(dist)
            build_extension.finalize_options()
            self._so_ext = build_extension.get_ext_filename('')
            return self._so_ext

# __doc__ = __doc__.format(FORTRAN_DOC=' ' * 8 + FortranMagics.fortran.__doc__)


def load_ipython_extension(ip):
    """Load the extension in IPython."""
    ip.register_magics(FortranMagics)

    # enable fortran highlight
    patch = ("IPython.config.cell_magic_highlight['magic_fortran'] = {'reg':[/^%%fortran/]};")
    js = display.Javascript(data=patch,
                            lib=["https://raw.github.com/marijnh/CodeMirror/master/mode/fortran/fortran.js"])
    display.display_javascript(js)

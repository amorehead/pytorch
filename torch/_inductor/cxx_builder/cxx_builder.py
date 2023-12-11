import errno
import functools
import os
import platform
import re
import shlex
import subprocess
import sys
import sysconfig
import warnings
from pathlib import Path
from typing import List, Tuple

import torch
from torch._inductor import config, exc
from torch._inductor.codecache import VecISA

if config.is_fbcode():
    from torch._inductor.fb.utils import (
        log_global_cache_errors,
        log_global_cache_stats,
        log_global_cache_vals,
        use_global_cache,
    )
else:

    def log_global_cache_errors(*args, **kwargs):
        pass

    def log_global_cache_stats(*args, **kwargs):
        pass

    def log_global_cache_vals(*args, **kwargs):
        pass

    def use_global_cache() -> bool:
        return False


# Windows need setup a temp dir to store .obj files.
_BUILD_TEMP_DIR = "CxxBuild"

# initialize variables for compilation
_IS_LINUX = sys.platform.startswith("linux")
_IS_MACOS = sys.platform.startswith("darwin")
_IS_WINDOWS = sys.platform == "win32"


def _get_cxx_compiler() -> str:
    if _IS_WINDOWS:
        compiler = os.environ.get("CXX", "cl")
    else:
        from torch._inductor.codecache import cpp_compiler

        compiler = cpp_compiler()
    return compiler


def _nonduplicate_append(dest_list: List[str], src_list: List[str]):
    for item in src_list:
        if item not in dest_list:
            dest_list.append(item)


def _remove_duplication_in_list(orig_list: List[str]) -> List[str]:
    new_list: List[str] = []
    for item in orig_list:
        if item not in new_list:
            new_list.append(item)
    return new_list


def _create_if_dir_not_exist(path_dir):
    if not os.path.exists(path_dir):
        try:
            Path(path_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # Guard against race condition
            if exc.errno != errno.EEXIST:
                raise RuntimeError(  # noqa: TRY200 (Use `raise from`)
                    f"Fail to create path {path_dir}"
                )


def _remove_dir(path_dir):
    if os.path.exists(path_dir):
        for root, dirs, files in os.walk(path_dir, topdown=False):
            for name in files:
                file_path = os.path.join(root, name)
                os.remove(file_path)
            for name in dirs:
                dir_path = os.path.join(root, name)
                os.rmdir(dir_path)
        os.rmdir(path_dir)


def run_command_line(cmd_line, cwd=None):
    cmd = shlex.split(cmd_line)
    try:
        status = subprocess.check_output(args=cmd, cwd=cwd, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        output = e.output.decode("utf-8")
        openmp_problem = "'omp.h' file not found" in output or "libomp" in output
        if openmp_problem and sys.platform == "darwin":
            instruction = (
                "\n\nOpenMP support not found. Please try one of the following solutions:\n"
                "(1) Set the `CXX` environment variable to a compiler other than Apple clang++/g++ "
                "that has builtin OpenMP support;\n"
                "(2) install OpenMP via conda: `conda install llvm-openmp`;\n"
                "(3) install libomp via brew: `brew install libomp`;\n"
                "(4) manually setup OpenMP and set the `OMP_PREFIX` environment variable to point to a path"
                " with `include/omp.h` under it."
            )
            output += instruction
        raise exc.CppCompileError(cmd, output) from e
    return status


def is_gcc(cpp_compiler) -> bool:
    return bool(re.search(r"(gcc|g\+\+)", cpp_compiler))


def is_clang(cpp_compiler) -> bool:
    return bool(re.search(r"(clang|clang\+\+)", cpp_compiler))


@functools.lru_cache(None)
def is_apple_clang(cpp_compiler) -> bool:
    version_string = subprocess.check_output([cpp_compiler, "--version"]).decode("utf8")
    return "Apple" in version_string.splitlines()[0]


class BuildOptionsBase:
    """
    This is the Base class for store cxx build options, as a template.
    Acturally, to build a cxx shared library. We just need to select a compiler
    and maintains the suitable args.
    This class will help maintains cxx build compiler and nessary args.
    """

    _compiler = ""
    _definations: List[str] = []
    _include_dirs: List[str] = []
    _cflags: List[str] = []
    _ldflags: List[str] = []
    _libraries_dirs: List[str] = []
    _libraries: List[str] = []
    _passthough_args: List[str] = []

    def _set_options(
        self,
        definations,
        include_dirs,
        cflags,
        ldflags,
        libraries_dirs,
        libraries,
        passthough_args,
    ):
        self._definations = definations
        self._include_dirs = include_dirs
        self._cflags = cflags
        self._ldflags = ldflags
        self._libraries_dirs = libraries_dirs
        self._libraries = libraries
        self._passthough_args = passthough_args

        self._definations = _remove_duplication_in_list(self._definations)
        self._include_dirs = _remove_duplication_in_list(self._include_dirs)
        self._cflags = _remove_duplication_in_list(self._cflags)
        self._ldflags = _remove_duplication_in_list(self._ldflags)
        self._libraries_dirs = _remove_duplication_in_list(self._libraries_dirs)
        self._libraries = _remove_duplication_in_list(self._libraries)
        self._passthough_args = _remove_duplication_in_list(self._passthough_args)

    def __init__(self) -> None:
        pass

    def get_compiler(self) -> str:
        return self._compiler

    def get_definations(self) -> List[str]:
        return self._definations

    def get_include_dirs(self) -> List[str]:
        return self._include_dirs

    def get_cflags(self) -> List[str]:
        return self._cflags

    def get_ldflags(self) -> List[str]:
        return self._ldflags

    def get_libraries_dirs(self) -> List[str]:
        return self._libraries_dirs

    def get_libraries(self) -> List[str]:
        return self._libraries

    def get_passthough_args(self) -> List[str]:
        return self._passthough_args


def _get_warning_all_cflag(warning_all: bool = True) -> List[str]:
    if not _IS_WINDOWS:
        return ["Wall"] if warning_all else []
    else:
        return []


def _get_cxx_std_cflag(std_num: str = "c++17") -> List[str]:
    if _IS_WINDOWS:
        return [f"std:{std_num}"]
    else:
        return [f"std={std_num}"]


def _get_linux_cpp_cflags(cpp_compiler) -> List[str]:
    if not _IS_WINDOWS:
        cflags = ["Wno-unused-variable", "Wno-unknown-pragmas"]
        if is_clang(cpp_compiler):
            cflags.append("Werror=ignored-optimization-argument")
        return cflags
    else:
        return []


def _get_optimization_cflags() -> List[str]:
    if _IS_WINDOWS:
        return ["O2"]
    else:
        cflags = ["O0", "g"] if config.aot_inductor.debug_compile else ["O3", "DNDEBUG"]
        cflags.append("ffast-math")
        cflags.append("fno-finite-math-only")

        if not config.cpp.enable_unsafe_math_opt_flag:
            cflags.append("fno-unsafe-math-optimizations")

        if config.is_fbcode():
            # FIXME: passing `-fopenmp` adds libgomp.so to the generated shared library's dependencies.
            # This causes `ldopen` to fail in fbcode, because libgomp does not exist in the default paths.
            # We will fix it later by exposing the lib path.
            return cflags

        if sys.platform == "darwin":
            # Per https://mac.r-project.org/openmp/ right way to pass `openmp` flags to MacOS is via `-Xclang`
            # Also, `-march=native` is unrecognized option on M1
            cflags.append("Xclang")
        else:
            if platform.machine() == "ppc64le":
                cflags.append("mcpu=native")
            else:
                cflags.append("march=native")

        # Internal cannot find libgomp.so
        # if not config.is_fbcode():
        #     cflags.append("fopenmp")

        return cflags


def _get_shared_cflag() -> List[str]:
    SHARED_FLAG = ["DLL"] if _IS_WINDOWS else ["shared", "fPIC"]
    return SHARED_FLAG


def get_cxx_options(cpp_compiler):
    definations: List[str] = []
    include_dirs: List[str] = []
    cflags: List[str] = []
    ldflags: List[str] = []
    libraries_dirs: List[str] = []
    libraries: List[str] = []
    passthough_args: List[str] = []

    cflags = (
        _get_shared_cflag()
        + _get_optimization_cflags()
        + _get_warning_all_cflag()
        + _get_cxx_std_cflag()
        + _get_linux_cpp_cflags(cpp_compiler)
    )

    return (
        definations,
        include_dirs,
        cflags,
        ldflags,
        libraries_dirs,
        libraries,
        passthough_args,
    )


class CxxOptions(BuildOptionsBase):
    """
    This class is inherited from BuildOptionsBase, and as cxx build options.
    This option contains basic cxx build option, which contains:
    1. OS related args.
    2. Toolchains related args.
    3. Cxx standard related args.
    Note:
    1. This Options is good for assist modules build, such as x86_isa_help.
    """

    def __init__(self) -> None:
        self._compiler = _get_cxx_compiler()

        (
            definations,
            include_dirs,
            cflags,
            ldflags,
            libraries_dirs,
            libraries,
            passthough_args,
        ) = get_cxx_options(self._compiler)

        self._set_options(
            definations,
            include_dirs,
            cflags,
            ldflags,
            libraries_dirs,
            libraries,
            passthough_args,
        )


def _get_glibcxx_abi_build_flags() -> List[str]:
    if not _IS_WINDOWS:
        return ["-D_GLIBCXX_USE_CXX11_ABI=" + str(int(torch._C._GLIBCXX_USE_CXX11_ABI))]
    else:
        return []


def _get_torch_cpp_wrapper_defination() -> List[str]:
    return ["TORCH_INDUCTOR_CPP_WRAPPER"]


def _use_custom_generated_macros() -> List[str]:
    return [" C10_USING_CUSTOM_GENERATED_MACROS"]


def _use_fb_internal_macros() -> List[str]:
    if not _IS_WINDOWS:
        if config.is_fbcode():
            # openmp_lib = build_paths.openmp_lib()
            preprocessor_flags = " ".join(
                (
                    "-D C10_USE_GLOG",
                    "-D C10_USE_MINIMAL_GLOG",
                    "-D C10_DISABLE_TENSORIMPL_EXTENSIBILITY",
                )
            )
            # return [f"-Wp,-fopenmp {openmp_lib} {preprocessor_flags}"]
            return [f"{preprocessor_flags}"]
        else:
            return []
    else:
        return []


def _use_standard_sys_dir_headers() -> List[str]:
    if _IS_WINDOWS:
        return []

    if config.is_fbcode():
        return ["nostdinc"]
    else:
        return []


@functools.lru_cache
def _cpp_prefix_path() -> str:
    from torch._inductor.codecache import write  # TODO

    path = Path(Path(__file__).parent).parent / "codegen/cpp_prefix.h"
    with path.open() as f:
        content = f.read()
        _, filename = write(
            content,
            "h",
        )
    return filename


def _get_build_args_of_chosen_isa(chosen_isa: VecISA):
    cap = str(chosen_isa).upper()
    macros = [
        f"CPU_CAPABILITY={cap}",
        f"CPU_CAPABILITY_{cap}",
        f"HAVE_{cap}_CPU_DEFINITION",
    ]
    # Add Windows support later.
    build_flags = [chosen_isa.build_arch_flags()]

    return macros, build_flags


def _get_torch_related_args(aot_mode: bool):
    from torch.utils.cpp_extension import _TORCH_PATH, TORCH_LIB_PATH

    include_dirs = [
        os.path.join(_TORCH_PATH, "include"),
        os.path.join(_TORCH_PATH, "include", "torch", "csrc", "api", "include"),
        # Some internal (old) Torch headers don't properly prefix their includes,
        # so we need to pass -Itorch/lib/include/TH as well.
        os.path.join(_TORCH_PATH, "include", "TH"),
        os.path.join(_TORCH_PATH, "include", "THC"),
    ]
    libraries_dirs = [TORCH_LIB_PATH]
    libraries = ["torch", "torch_cpu", "c10"]
    if not aot_mode:
        libraries.append("torch_python")
    return include_dirs, libraries_dirs, libraries


def _get_python_related_args():
    python_include_dirs = []
    python_include_path = sysconfig.get_path(
        "include", scheme="nt" if _IS_WINDOWS else "posix_prefix"
    )
    if python_include_path is not None:
        python_include_dirs.append(python_include_path)

    if _IS_WINDOWS:
        python_path = os.path.dirname(sys.executable)
        python_lib_path = [os.path.join(python_path, "libs")]
    else:
        python_lib_path = [sysconfig.get_config_var("LIBDIR")]

    return python_include_dirs, python_lib_path


def _get_openmp_args(cpp_compiler):
    cflags: List[str] = []
    ldflags: List[str] = []
    include_dir_paths: List[str] = []
    lib_dir_paths: List[str] = []
    libs: List[str] = []
    if _IS_MACOS:
        from torch._inductor.codecache import (
            homebrew_libomp,
            is_conda_llvm_openmp_installed,
        )

        # only Apple builtin compilers (Apple Clang++) require openmp
        omp_available = not is_apple_clang(cpp_compiler)

        # check the `OMP_PREFIX` environment first
        omp_prefix = os.getenv("OMP_PREFIX")
        if omp_prefix is not None:
            header_path = os.path.join(omp_prefix, "include", "omp.h")
            valid_env = os.path.exists(header_path)
            if valid_env:
                include_dir_paths.append(os.path.join(omp_prefix, "include"))
                lib_dir_paths.append(os.path.join(omp_prefix, "lib"))
            else:
                warnings.warn("environment variable `OMP_PREFIX` is invalid.")
            omp_available = omp_available or valid_env

        if not omp_available:
            libs.append("omp")

        # prefer to use openmp from `conda install llvm-openmp`
        conda_prefix = os.getenv("CONDA_PREFIX")
        if not omp_available and conda_prefix is not None:
            omp_available = is_conda_llvm_openmp_installed()
            if omp_available:
                conda_lib_path = os.path.join(conda_prefix, "lib")
                include_dir_paths.append(os.path.join(conda_prefix, "include"))
                lib_dir_paths.append(conda_lib_path)
                # Prefer Intel OpenMP on x86 machine
                if os.uname().machine == "x86_64" and os.path.exists(
                    os.path.join(conda_lib_path, "libiomp5.dylib")
                ):
                    libs.append("iomp5")

        # next, try to use openmp from `brew install libomp`
        if not omp_available:
            omp_available, libomp_path = homebrew_libomp()
            if omp_available:
                include_dir_paths.append(os.path.join(libomp_path, "include"))
                lib_dir_paths.append(os.path.join(libomp_path, "lib"))

        # if openmp is still not available, we let the compiler to have a try,
        # and raise error together with instructions at compilation error later
    elif _IS_WINDOWS:
        # /openmp, /openmp:llvm
        # llvm on Windows, new openmp: https://devblogs.microsoft.com/cppblog/msvc-openmp-update/
        # msvc openmp: https://learn.microsoft.com/zh-cn/cpp/build/reference/openmp-enable-openmp-2-0-support?view=msvc-170

        cflags.append("openmp")
        libs = []
    else:
        if config.is_fbcode():
            libs.append("omp")
        else:
            if is_clang(cpp_compiler):
                # TODO: fix issue, can't find omp.h
                cflags.append("fopenmp")
                libs.append("gomp")
            else:
                cflags.append("fopenmp")
                libs.append("gomp")

    return cflags, ldflags, include_dir_paths, lib_dir_paths, libs


def get_cxx_torch_options(cpp_compiler, chosen_isa: VecISA, aot_mode: bool = False):
    definations: List[str] = []
    include_dirs: List[str] = []
    cflags: List[str] = []
    ldflags: List[str] = []
    libraries_dirs: List[str] = []
    libraries: List[str] = []
    passthough_args: List[str] = []

    torch_cpp_wrapper_definations = _get_torch_cpp_wrapper_defination()
    use_custom_generated_macros_definations = _use_custom_generated_macros()

    sys_dir_header_cflags = _use_standard_sys_dir_headers()

    isa_macros, isa_ps_args_build_flags = _get_build_args_of_chosen_isa(chosen_isa)

    (
        torch_include_dirs,
        torch_libraries_dirs,
        torch_libraries,
    ) = _get_torch_related_args(aot_mode)

    python_include_dirs, python_libraries_dirs = _get_python_related_args()

    (
        omp_cflags,
        omp_ldflags,
        omp_include_dir_paths,
        omp_lib_dir_paths,
        omp_lib,
    ) = _get_openmp_args(cpp_compiler)

    cxx_abi_passthough_args = _get_glibcxx_abi_build_flags()
    fb_macro_passthough_args = _use_fb_internal_macros()

    definations = (
        torch_cpp_wrapper_definations
        + use_custom_generated_macros_definations
        + isa_macros
        + fb_macro_passthough_args
    )
    include_dirs = python_include_dirs + torch_include_dirs + omp_include_dir_paths
    cflags = sys_dir_header_cflags + omp_cflags
    ldflags = omp_ldflags
    libraries_dirs = python_libraries_dirs + torch_libraries_dirs + omp_lib_dir_paths
    libraries = torch_libraries + omp_lib
    passthough_args = isa_ps_args_build_flags + cxx_abi_passthough_args

    return (
        definations,
        include_dirs,
        cflags,
        ldflags,
        libraries_dirs,
        libraries,
        passthough_args,
    )


class CxxTorchOptions(CxxOptions):
    """
    This class is inherited from CxxTorchOptions, which need contains
    base cxx build options. And then it will maintains torch related build
    args.
    1. Torch include_directories, libraries, libraries_directories.
    2. Python include_directories, libraries, libraries_directories.
    3. OpenMP related.
    4. Torch MACROs.
    5. MISC
    """

    def __init__(self, chosen_isa: VecISA, aot_mode: bool = False) -> None:
        self._compiler = _get_cxx_compiler()

        (
            cxx_definations,
            cxx_include_dirs,
            cxx_cflags,
            cxx_ldflags,
            cxx_libraries_dirs,
            cxx_libraries,
            cxx_passthough_args,
        ) = get_cxx_options(self._compiler)

        (
            torch_definations,
            torch_include_dirs,
            torch_cflags,
            torch_ldflags,
            torch_libraries_dirs,
            torch_libraries,
            torch_passthough_args,
        ) = get_cxx_torch_options(cpp_compiler=self._compiler, chosen_isa=chosen_isa)

        definations = cxx_definations + torch_definations
        include_dirs = cxx_include_dirs + torch_include_dirs
        cflags = cxx_cflags + torch_cflags
        ldflags = cxx_ldflags + torch_ldflags
        libraries_dirs = cxx_libraries_dirs + torch_libraries_dirs
        libraries = cxx_libraries + torch_libraries
        passthough_args = cxx_passthough_args + torch_passthough_args

        self._set_options(
            definations,
            include_dirs,
            cflags,
            ldflags,
            libraries_dirs,
            libraries,
            passthough_args,
        )


def _get_cuda_related_args(aot_mode: bool):
    definations: List[str] = []
    include_dirs: List[str] = []
    cflags: List[str] = []
    ldflags: List[str] = []
    libraries_dirs: List[str] = []
    libraries: List[str] = []
    passthough_args: List[str] = []

    use_cuda = True

    from torch.utils import cpp_extension

    include_dirs = cpp_extension.include_paths(use_cuda)
    libraries_dirs = cpp_extension.library_paths(use_cuda)

    if torch.version.hip is not None:
        libraries += ["c10_hip", "torch_hip"]
    else:
        if config.is_fbcode():
            libraries += ["cuda"]
        else:
            libraries += ["c10_cuda", "cuda", "torch_cuda"]

    if aot_mode:
        cpp_prefix_include_dir = [f"{os.path.dirname(_cpp_prefix_path())}"]
        include_dirs += cpp_prefix_include_dir
        definations.append("USE_CUDA")

        if not _IS_WINDOWS:
            # TODO: make static link better on Linux.
            passthough_args = ["-Wl,-Bstatic -lcudart_static -Wl,-Bdynamic"]
        else:
            libraries.append("cudart_static")

    return (
        definations,
        include_dirs,
        cflags,
        ldflags,
        libraries_dirs,
        libraries,
        passthough_args,
    )


def get_cxx_torch_cuda_options(aot_mode: bool = False):
    definations: List[str] = []
    include_dirs: List[str] = []
    cflags: List[str] = []
    ldflags: List[str] = []
    libraries_dirs: List[str] = []
    libraries: List[str] = []
    passthough_args: List[str] = []

    (
        definations,
        include_dirs,
        cflags,
        ldflags,
        libraries_dirs,
        libraries,
        passthough_args,
    ) = _get_cuda_related_args(aot_mode)

    return (
        definations,
        include_dirs,
        cflags,
        ldflags,
        libraries_dirs,
        libraries,
        passthough_args,
    )


class CxxTorchCudaOptions(CxxTorchOptions):
    """
    This class is inherited from CxxTorchOptions, which need contains
    base cxx build options and torch common build options. And then it will
    maintains cuda device related build args.
    """

    def __init__(self, use_cuda: bool = True, aot_mode: bool = False) -> None:
        self._compiler = _get_cxx_compiler()
        from torch._inductor.codecache import pick_vec_isa

        chosen_isa = pick_vec_isa()

        (
            cxx_definations,
            cxx_include_dirs,
            cxx_cflags,
            cxx_ldflags,
            cxx_libraries_dirs,
            cxx_libraries,
            cxx_passthough_args,
        ) = get_cxx_options(self._compiler)

        (
            torch_definations,
            torch_include_dirs,
            torch_cflags,
            torch_ldflags,
            torch_libraries_dirs,
            torch_libraries,
            torch_passthough_args,
        ) = get_cxx_torch_options(
            cpp_compiler=self._compiler, chosen_isa=chosen_isa, aot_mode=aot_mode
        )

        cuda_definations: List[str] = []
        cuda_include_dirs: List[str] = []
        cuda_cflags: List[str] = []
        cuda_ldflags: List[str] = []
        cuda_libraries_dirs: List[str] = []
        cuda_libraries: List[str] = []
        cuda_passthough_args: List[str] = []

        if use_cuda:
            (
                cuda_definations,
                cuda_include_dirs,
                cuda_cflags,
                cuda_ldflags,
                cuda_libraries_dirs,
                cuda_libraries,
                cuda_passthough_args,
            ) = get_cxx_torch_cuda_options(aot_mode=aot_mode)

        definations = cxx_definations + torch_definations + cuda_definations
        include_dirs = cxx_include_dirs + torch_include_dirs + cuda_include_dirs
        cflags = cxx_cflags + torch_cflags + cuda_cflags
        ldflags = cxx_ldflags + torch_ldflags + cuda_ldflags
        libraries_dirs = cxx_libraries_dirs + torch_libraries_dirs + cuda_libraries_dirs
        libraries = cxx_libraries + torch_libraries + cuda_libraries
        passthough_args = (
            cxx_passthough_args + torch_passthough_args + cuda_passthough_args
        )

        self._set_options(
            definations,
            include_dirs,
            cflags,
            ldflags,
            libraries_dirs,
            libraries,
            passthough_args,
        )


class CxxBuilder:
    _compiler = ""
    _cflags_args = ""
    _definations_args = ""
    _include_dirs_args = ""
    _ldflags_args = ""
    _libraries_dirs_args = ""
    _libraries_args = ""
    _passthough_parameters_args = ""

    _name = ""
    _sources_args = ""
    _output_dir = ""
    _target_file = ""

    _compile_only = False

    def get_shared_lib_ext(self) -> str:
        SHARED_LIB_EXT = ".dll" if _IS_WINDOWS else ".so"
        return SHARED_LIB_EXT

    def get_object_ext(self) -> str:
        EXT = ".obj" if _IS_WINDOWS else ".o"
        return EXT

    def __init__(
        self,
        name: str,
        sources: List[str],
        BuildOption: BuildOptionsBase,
        output_dir: str = "",
        compile_only: bool = False,
    ) -> None:
        self._name = name
        self._sources_args = " ".join(sources)

        self._compile_only = compile_only

        if output_dir is None:
            self._output_dir = os.path.dirname(os.path.abspath(__file__))
        else:
            self._output_dir = output_dir

        file_ext = self.get_object_ext() if compile_only else self.get_shared_lib_ext()
        self._target_file = os.path.join(self._output_dir, f"{self._name}{file_ext}")

        self._compiler = BuildOption.get_compiler()

        for cflag in BuildOption.get_cflags():
            if _IS_WINDOWS:
                self._cflags_args += f"/{cflag} "
            else:
                self._cflags_args += f"-{cflag} "

        for defination in BuildOption.get_definations():
            if _IS_WINDOWS:
                self._definations_args += f"/D {defination} "
            else:
                self._definations_args += f"-D{defination} "

        for inc_dir in BuildOption.get_include_dirs():
            if _IS_WINDOWS:
                self._include_dirs_args += f"/I {inc_dir} "
            else:
                self._include_dirs_args += f"-I{inc_dir} "

        for ldflag in BuildOption.get_ldflags():
            if _IS_WINDOWS:
                self._ldflags_args += f"/{ldflag} "
            else:
                self._ldflags_args += f"-{ldflag} "

        for lib_dir in BuildOption.get_libraries_dirs():
            if _IS_WINDOWS:
                self._libraries_dirs_args += f'/LIBPATH:"{lib_dir}" '
            else:
                self._libraries_dirs_args += f"-L{lib_dir} "

        for lib in BuildOption.get_libraries():
            if _IS_WINDOWS:
                self._libraries_args += f'"{lib}.lib" '
            else:
                self._libraries_args += f"-l{lib} "

        for passthough_arg in BuildOption.get_passthough_args():
            self._passthough_parameters_args += f"{passthough_arg} "

    def get_command_line(self) -> str:
        def format_build_command(
            compiler,
            sources,
            include_dirs_args,
            definations_args,
            cflags_args,
            ldflags_args,
            libraries_args,
            libraries_dirs_args,
            passthougn_args,
            target_file,
        ):
            if _IS_WINDOWS:
                # https://learn.microsoft.com/en-us/cpp/build/walkthrough-compile-a-c-program-on-the-command-line?view=msvc-1704
                # https://stackoverflow.com/a/31566153
                cmd = (
                    f"{compiler} {include_dirs_args} {definations_args} {cflags_args} {sources} "
                    f"{passthougn_args} /LD /Fe{target_file} /link {libraries_dirs_args} {libraries_args} {ldflags_args} "
                )
                cmd = cmd.replace("\\", "/")
            else:
                compile_only_arg = "-c" if self._compile_only else ""
                cmd = re.sub(
                    r"[ \n]+",
                    " ",
                    f"""
                    {compiler} {sources} {definations_args} {cflags_args} {include_dirs_args}
                    {passthougn_args} {ldflags_args} {libraries_args} {libraries_dirs_args} {compile_only_arg} -o {target_file}
                    """,
                ).strip()
            return cmd

        command_line = format_build_command(
            compiler=self._compiler,
            sources=self._sources_args,
            include_dirs_args=self._include_dirs_args,
            definations_args=self._definations_args,
            cflags_args=self._cflags_args,
            ldflags_args=self._ldflags_args,
            libraries_args=self._libraries_args,
            libraries_dirs_args=self._libraries_dirs_args,
            passthougn_args=self._passthough_parameters_args,
            target_file=self._target_file,
        )
        return command_line

    def get_target_file_path(self):
        return self._target_file

    def build(self) -> Tuple[int, str]:
        """
        It is must need a temperary directory to store object files in Windows.
        """
        _create_if_dir_not_exist(self._output_dir)
        _build_tmp_dir = os.path.join(
            self._output_dir, f"{self._name}_{_BUILD_TEMP_DIR}"
        )
        _create_if_dir_not_exist(_build_tmp_dir)

        build_cmd = self.get_command_line()
        print("!!! build_cmd: ", build_cmd)
        status = run_command_line(build_cmd, cwd=_build_tmp_dir)

        _remove_dir(_build_tmp_dir)
        return status, self._target_file

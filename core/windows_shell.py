"""Reveal a file using Windows Explorer's native item selection."""

import ctypes
from ctypes import wintypes


def reveal_file(path):
    ole = ctypes.WinDLL('ole32')
    shell = ctypes.WinDLL('shell32')
    ole.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = ()
    ole.CoUninitialize.restype = None
    ole.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    ole.CoTaskMemFree.restype = None
    parse = shell.SHParseDisplayName
    parse.argtypes = (wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    parse.restype = ctypes.c_long
    select = shell.SHOpenFolderAndSelectItems
    select.argtypes = (ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD)
    select.restype = ctypes.c_long

    initialized = ole.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    already_initialized = ctypes.c_long(0x80010106).value  # RPC_E_CHANGED_MODE
    if initialized < 0 and initialized != already_initialized:
        raise OSError('无法初始化 Windows 资源管理器')

    item = ctypes.c_void_p()
    try:
        if parse(str(path), None, ctypes.byref(item), 0, None) < 0 or not item:
            raise OSError('资源管理器无法定位导出的文件')
        # An absolute item with a zero child count selects it in its parent.
        if select(item, 0, None, 0) < 0:
            raise OSError('资源管理器无法选中导出的文件')
    finally:
        if item:
            ole.CoTaskMemFree(item)
        if initialized >= 0:
            ole.CoUninitialize()

#include <Python.h>

#include "EDSDKTypes.h"


#define PyCheck_EDSERROR(err) \
    do { \
        if ((err) != EDS_ERR_OK) { \
            PyObject *exc_args = Py_BuildValue( \
                "(sk)", EDS::errorMessage(err), static_cast<unsigned long>(err)); \
            if (exc_args != nullptr) { \
                PyErr_SetObject(PyEdsError, exc_args); \
                Py_DECREF(exc_args); \
            } \
            return nullptr; \
        } \
    } while (0)


namespace EDS {
    char const *errorMessage(EdsError const error);

    PyObject *PyDict_FromEdsPoint(EdsPoint const &point);
    PyObject *PyDict_FromEdsSize(EdsSize const &size);
    PyObject *PyDict_FromEdsRect(EdsRect const &rect);
    PyObject *PyDict_FromEdsImageInfo(EdsImageInfo const &imageInfo);

    bool PyDict_ToEdsPoint(PyObject *pyDict, EdsPoint &point);
    bool PyDict_ToEdsSize(PyObject *pyDict, EdsSize &size);
    bool PyDict_ToEdsRect(PyObject *pyDict, EdsRect &rect);
}

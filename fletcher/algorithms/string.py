from functools import singledispatch
from typing import Any, List, Tuple

import numpy as np
import pyarrow as pa
from numba import njit

from fletcher._algorithms import (
    _buffer_to_view,
    _calculate_chunk_offsets,
    _combined_in_chunk_offsets,
    _merge_valid_bitmaps,
    apply_per_chunk,
)


def _extract_string_buffers(arr: pa.Array) -> Tuple[np.ndarray, np.ndarray]:
    start = arr.offset
    end = arr.offset + len(arr)

    offsets = np.asanyarray(arr.buffers()[1]).view(np.int32)[start : end + 1]
    data = np.asanyarray(arr.buffers()[2]).view(np.uint8)

    return offsets, data


@njit
def _merge_string_data(
    length: int,
    valid_bits: np.ndarray,
    offsets_a: np.ndarray,
    data_a: np.ndarray,
    offsets_b: np.ndarray,
    data_b: np.ndarray,
    result_offsets: np.ndarray,
    result_data: np.ndarray,
) -> None:
    for i in range(length):
        byte_offset = i // 8
        bit_offset = i % 8
        mask = np.uint8(1 << bit_offset)
        valid = valid_bits[byte_offset] & mask

        if not valid:
            result_offsets[i + 1] = result_offsets[i]
        else:
            len_a = offsets_a[i + 1] - offsets_a[i]
            len_b = offsets_b[i + 1] - offsets_b[i]
            result_offsets[i + 1] = result_offsets[i] + len_a + len_b
            for j in range(len_a):
                result_data[result_offsets[i] + j] = data_a[offsets_a[i] + j]
            for j in range(len_b):
                result_data[result_offsets[i] + len_a + j] = data_b[offsets_b[i] + j]


@singledispatch
def _text_cat_chunked(a: Any, b: pa.ChunkedArray) -> pa.ChunkedArray:
    raise NotImplementedError(
        "_text_cat_chunked is only implemented for pa.Array and pa.ChunkedArray"
    )


@_text_cat_chunked.register(pa.ChunkedArray)
def _text_cat_chunked_1(a: pa.ChunkedArray, b: pa.ChunkedArray) -> pa.ChunkedArray:
    in_a_offsets, in_b_offsets = _combined_in_chunk_offsets(a, b)

    new_chunks: List[pa.Array] = []
    for a_offset, b_offset in zip(in_a_offsets, in_b_offsets):
        a_slice = a.chunk(a_offset[0])[a_offset[1] : a_offset[1] + a_offset[2]]
        b_slice = b.chunk(b_offset[0])[b_offset[1] : b_offset[1] + b_offset[2]]
        new_chunks.append(_text_cat(a_slice, b_slice))
    return pa.chunked_array(new_chunks)


@_text_cat_chunked.register(pa.Array)
def _text_cat_chunked_2(a: pa.Array, b: pa.ChunkedArray) -> pa.ChunkedArray:
    new_chunks = []
    offsets = _calculate_chunk_offsets(b)
    for chunk, offset in zip(b.iterchunks(), offsets):
        new_chunks.append(_text_cat(a[offset : offset + len(chunk)], chunk))
    return pa.chunked_array(new_chunks)


def _text_cat_chunked_mixed(a: pa.ChunkedArray, b: pa.Array) -> pa.ChunkedArray:
    new_chunks = []
    offsets = _calculate_chunk_offsets(a)
    for chunk, offset in zip(a.iterchunks(), offsets):
        new_chunks.append(_text_cat(chunk, b[offset : offset + len(chunk)]))
    return pa.chunked_array(new_chunks)


def _text_cat(a: pa.Array, b: pa.Array) -> pa.Array:
    if len(a) != len(b):
        raise ValueError("Lengths of arrays don't match")

    offsets_a, data_a = _extract_string_buffers(a)
    offsets_b, data_b = _extract_string_buffers(b)
    if len(a) > 0:
        valid = _merge_valid_bitmaps(a, b)
        result_offsets = np.empty(len(a) + 1, dtype=np.int32)
        result_offsets[0] = 0
        total_size = (offsets_a[-1] - offsets_a[0]) + (offsets_b[-1] - offsets_b[0])
        result_data = np.empty(total_size, dtype=np.uint8)
        _merge_string_data(
            len(a),
            valid,
            offsets_a,
            data_a,
            offsets_b,
            data_b,
            result_offsets,
            result_data,
        )
        buffers = [pa.py_buffer(x) for x in [valid, result_offsets, result_data]]
        return pa.Array.from_buffers(pa.string(), len(a), buffers)
    return a


@njit
def _text_contains_case_sensitive_nonnull(length, offsets, data, pat, output):
    for row_idx in range(length):
        str_len = offsets[row_idx + 1] - offsets[row_idx]

        contains = False
        for str_idx in range(max(0, str_len - len(pat) + 1)):
            pat_found = True
            for pat_idx in range(len(pat)):
                if data[offsets[row_idx] + str_idx + pat_idx] != pat[pat_idx]:
                    pat_found = False
                    break
            if pat_found:
                contains = True
                break

        # TODO: Set word-wise for better performance
        byte_offset_result = row_idx // 8
        bit_offset_result = row_idx % 8
        mask_result = np.uint8(1 << bit_offset_result)
        current = output[byte_offset_result]
        if contains:  # must be logical, not bit-wise as different bits may be flagged
            output[byte_offset_result] = current | mask_result
        else:
            output[byte_offset_result] = current & ~mask_result


@njit
def _text_contains_case_sensitive_nulls(
    length, valid_bits, valid_offset, offsets, data, pat, output
):
    for row_idx in range(length):
        byte_offset = (row_idx + valid_offset) // 8
        bit_offset = (row_idx + valid_offset) % 8
        mask = np.uint8(1 << bit_offset)
        valid = valid_bits[byte_offset] & mask

        if not valid:
            continue

        str_len = offsets[row_idx + 1] - offsets[row_idx]

        contains = False
        for str_idx in range(max(0, str_len - len(pat) + 1)):
            pat_found = True
            for pat_idx in range(len(pat)):
                if data[offsets[row_idx] + str_idx + pat_idx] != pat[pat_idx]:
                    pat_found = False
                    break
            if pat_found:
                contains = True
                break

        # TODO: Set word-wise for better performance
        byte_offset_result = row_idx // 8
        bit_offset_result = row_idx % 8
        mask_result = np.uint8(1 << bit_offset_result)
        current = output[byte_offset_result]
        if contains:  # must be logical, not bit-wise as different bits may be flagged
            output[byte_offset_result] = current | mask_result
        else:
            output[byte_offset_result] = current & ~mask_result


@njit
def _shift_unaligned_bitmap(valid_bits, valid_offset, length, output):
    for i in range(length):
        byte_offset = (i + valid_offset) // 8
        bit_offset = (i + valid_offset) % 8
        mask = np.uint8(1 << bit_offset)
        valid = valid_bits[byte_offset] & mask

        byte_offset_result = i // 8
        bit_offset_result = i % 8
        mask_result = np.uint8(1 << bit_offset_result)
        current = output[byte_offset_result]
        if valid:
            output[byte_offset_result] = current | mask_result


def shift_unaligned_bitmap(valid_buffer, offset, length):
    """Shift an unaligned bitmap to be offsetted at 0."""
    output_size = length // 8
    if length % 8 > 0:
        output_size += 1
    output = np.zeros(output_size, dtype=np.uint8)

    _shift_unaligned_bitmap(valid_buffer, offset, length, output)

    return pa.py_buffer(output)


@apply_per_chunk
def _text_contains_case_sensitive(data: pa.Array, pat: str) -> pa.Array:

    output_size = len(data) // 8
    if len(data) % 8 > 0:
        output_size += 1
    output = np.empty(output_size, dtype=np.uint8)
    if len(data) % 8 > 0:
        # Zero trailing bits
        output[-1] = 0

    offsets, data_buffer = _extract_string_buffers(data)
    # Convert to UTF-8 bytes
    pat_bytes = pat.encode()

    if data.null_count == 0:
        valid_buffer = None
        _text_contains_case_sensitive_nonnull(
            len(data), offsets, data_buffer, pat_bytes, output
        )
    else:
        valid = _buffer_to_view(data.buffers()[0])
        _text_contains_case_sensitive_nulls(
            len(data), valid, data.offset, offsets, data_buffer, pat_bytes, output
        )
        valid_buffer = data.buffers()[0].slice(data.offset // 8)
        if data.offset % 8 != 0:
            valid_buffer = shift_unaligned_bitmap(
                valid_buffer, data.offset % 8, len(data)
            )

    return pa.Array.from_buffers(
        pa.bool_(), len(data), [valid_buffer, pa.py_buffer(output)], data.null_count
    )

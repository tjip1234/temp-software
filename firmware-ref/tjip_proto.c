/*
 * tjip_proto.c — reference implementation of the TJIP-1 wire protocol.
 * See tjip_proto.h and docs/protocol.md.
 */

#include "tjip_proto.h"

#include <stdio.h>
#include <string.h>

/* ------------------------------------------------------------------ checksums */

uint16_t tjip_crc16(const uint8_t *data, size_t len)
{
    /* Bitwise rather than table-driven: 512 bytes of flash matters more on an
     * embedded target than the handful of microseconds this costs, and frames
     * are short. Swap in a table if you ever profile it as hot. */
    uint16_t crc = 0xFFFFu;
    for (size_t i = 0; i < len; ++i) {
        crc ^= (uint16_t)data[i] << 8;
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

/* ----------------------------------------------------------------------- COBS */

size_t tjip_cobs_encode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap)
{
    if (out_cap < len + len / 254u + 2u) {
        return 0;
    }

    size_t read = 0;
    size_t write = 1;      /* index 0 holds the first code byte */
    size_t code_index = 0;
    uint8_t code = 1;

    while (read < len) {
        const uint8_t byte = in[read++];
        if (byte != 0) {
            out[write++] = byte;
            /* A run of 254 non-zero bytes fills the block; close it and start a
             * new one. Note the input byte has already been consumed either way,
             * which is exactly the case the obvious loop gets wrong. */
            if (++code != 0xFF) {
                continue;
            }
        }
        out[code_index] = code;
        code_index = write++;
        code = 1;
    }
    out[code_index] = code;
    return write;
}

size_t tjip_cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap)
{
    size_t read = 0;
    size_t write = 0;

    while (read < len) {
        uint8_t code = in[read];
        if (code == 0) {
            return 0;  /* a zero code byte cannot occur inside a valid block */
        }
        ++read;
        size_t run = (size_t)code - 1u;
        if (read + run > len || write + run > out_cap) {
            return 0;
        }
        memcpy(out + write, in + read, run);
        write += run;
        read += run;
        if (code != 0xFF && read < len) {
            if (write >= out_cap) {
                return 0;
            }
            out[write++] = 0;
        }
    }
    return write;
}

/* --------------------------------------------------------------------- frames */

size_t tjip_encode(uint8_t msg_type, uint8_t flags, uint16_t seq,
                   const void *payload, size_t payload_len,
                   uint8_t *out, size_t out_cap)
{
    if (payload_len > TJIP_MAX_PAYLOAD) {
        return 0;
    }

    uint8_t raw[TJIP_HEADER_SIZE + TJIP_MAX_PAYLOAD + TJIP_CRC_SIZE];
    raw[0] = msg_type;
    raw[1] = flags;
    tjip_put_u16(raw + 2, seq);
    tjip_put_u16(raw + 4, (uint16_t)payload_len);
    if (payload_len > 0 && payload != NULL) {
        memcpy(raw + TJIP_HEADER_SIZE, payload, payload_len);
    }

    const size_t body = TJIP_HEADER_SIZE + payload_len;
    tjip_put_u16(raw + body, tjip_crc16(raw, body));

    const size_t encoded = tjip_cobs_encode(raw, body + TJIP_CRC_SIZE, out, out_cap);
    if (encoded == 0 || encoded + 1u > out_cap) {
        return 0;
    }
    out[encoded] = 0x00;  /* delimiter */
    return encoded + 1u;
}

size_t tjip_encode_response(uint8_t msg_type, uint16_t seq,
                            const char *json, size_t json_len,
                            uint8_t *out, size_t out_cap)
{
    return tjip_encode(msg_type, TJIP_FLAG_RESPONSE, seq, json, json_len, out, out_cap);
}

size_t tjip_encode_error(uint16_t seq, const char *code, const char *msg,
                         uint8_t *out, size_t out_cap)
{
    char json[256];
    const int n = snprintf(json, sizeof json,
                           "{\"code\":\"%s\",\"msg\":\"%s\",\"detail\":{}}",
                           code ? code : "unknown", msg ? msg : "");
    if (n <= 0) {
        return 0;
    }
    size_t len = (size_t)n;
    if (len >= sizeof json) {
        len = sizeof json - 1u;
    }
    return tjip_encode(TJIP_MSG_ERROR, TJIP_FLAG_RESPONSE | TJIP_FLAG_ERROR, seq,
                       json, len, out, out_cap);
}

/* --------------------------------------------------------------------- reader */

void tjip_reader_init(tjip_reader_t *reader, uint8_t *scratch, size_t scratch_cap)
{
    memset(reader, 0, sizeof *reader);
    reader->scratch = scratch;
    reader->scratch_cap = scratch_cap;
}

void tjip_reader_reset(tjip_reader_t *reader)
{
    reader->fill = 0;
    reader->overflow = false;
}

static bool tjip_reader_finish(tjip_reader_t *reader, tjip_frame_t *out_frame)
{
    /* Decode in place is not safe here (COBS output is shorter than its input
     * but the runs overlap), so decode into the tail of the scratch buffer. */
    const size_t block_len = reader->fill;
    if (block_len == 0) {
        return false;  /* back-to-back delimiters, or leading padding */
    }

    uint8_t decoded[TJIP_HEADER_SIZE + TJIP_MAX_PAYLOAD + TJIP_CRC_SIZE];
    const size_t raw_len = tjip_cobs_decode(reader->scratch, block_len,
                                            decoded, sizeof decoded);
    reader->fill = 0;

    if (raw_len < TJIP_HEADER_SIZE + TJIP_CRC_SIZE) {
        reader->bad_frames++;
        reader->dropped_bytes += (uint32_t)block_len;
        return false;
    }

    const uint16_t payload_len = tjip_get_u16(decoded + 4);
    if ((size_t)payload_len + TJIP_HEADER_SIZE + TJIP_CRC_SIZE != raw_len) {
        reader->bad_frames++;
        reader->dropped_bytes += (uint32_t)block_len;
        return false;
    }

    const size_t body = TJIP_HEADER_SIZE + payload_len;
    if (tjip_get_u16(decoded + body) != tjip_crc16(decoded, body)) {
        reader->bad_frames++;
        reader->dropped_bytes += (uint32_t)block_len;
        return false;
    }

    /* Copy the payload back into scratch so it outlives `decoded`. */
    if (payload_len > 0) {
        memcpy(reader->scratch, decoded + TJIP_HEADER_SIZE, payload_len);
    }
    out_frame->msg_type    = decoded[0];
    out_frame->flags       = decoded[1];
    out_frame->seq         = tjip_get_u16(decoded + 2);
    out_frame->payload     = reader->scratch;
    out_frame->payload_len = payload_len;
    reader->good_frames++;
    return true;
}

bool tjip_reader_feed(tjip_reader_t *reader, const uint8_t *data, size_t len,
                      size_t *consumed, tjip_frame_t *out_frame)
{
    size_t i = 0;
    while (i < len) {
        const uint8_t byte = data[i++];
        if (byte == 0x00) {
            if (reader->overflow) {
                /* The block we were skipping has ended; resynchronise here. */
                reader->overflow = false;
                reader->fill = 0;
                reader->bad_frames++;
                continue;
            }
            const bool got = tjip_reader_finish(reader, out_frame);
            if (got) {
                *consumed = i;
                return true;
            }
            continue;
        }

        if (reader->overflow) {
            reader->dropped_bytes++;
            continue;
        }
        if (reader->fill >= reader->scratch_cap) {
            /* Implausibly long run with no delimiter: the stream is junk. Skip
             * to the next delimiter rather than corrupting a later good frame. */
            reader->overflow = true;
            reader->dropped_bytes += (uint32_t)reader->fill;
            reader->fill = 0;
            continue;
        }
        reader->scratch[reader->fill++] = byte;
    }
    *consumed = len;
    return false;
}

/* --------------------------------------------------------------- sample blocks */

static size_t tjip_block_data_offset(uint8_t n_ch)
{
    const size_t unpadded = TJIP_BLOCK_HEAD_SIZE + (size_t)n_ch;
    return unpadded + ((4u - (unpadded % 4u)) % 4u);
}

size_t tjip_block_size(uint8_t n_ch, uint16_t n_samples)
{
    return tjip_block_data_offset(n_ch) + (size_t)n_samples * n_ch * sizeof(float);
}

uint16_t tjip_block_rows_for(uint8_t n_ch, size_t payload_budget)
{
    if (n_ch == 0) {
        return 0;
    }
    const size_t offset = tjip_block_data_offset(n_ch);
    if (payload_budget <= offset) {
        return 0;
    }
    const size_t rows = (payload_budget - offset) / ((size_t)n_ch * sizeof(float));
    return rows > 0xFFFFu ? 0xFFFFu : (uint16_t)rows;
}

size_t tjip_block_write_header(const tjip_block_header_t *header,
                               uint8_t *out, size_t out_cap)
{
    const size_t offset = tjip_block_data_offset(header->n_ch);
    if (header->n_ch == 0 || out_cap < offset) {
        return 0;
    }

    uint8_t flags = header->flags;
    const uint16_t step = header->seq_step ? header->seq_step : 1u;
    if (step != 1u) {
        flags |= TJIP_BLOCK_FLAG_DECIMATED;
    }

    out[0] = TJIP_BLOCK_VERSION;
    out[1] = header->n_ch;
    tjip_put_u16(out + 2, header->n_samples);
    tjip_put_u32(out + 4, header->first_seq);
    tjip_put_u64(out + 8, header->t0_us);
    tjip_put_u32(out + 16, header->dt_us);
    tjip_put_u32(out + 20, header->fault_mask);
    out[24] = flags;
    out[25] = 0;  /* reserved */
    tjip_put_u16(out + 26, step);
    memcpy(out + TJIP_BLOCK_HEAD_SIZE, header->channel_ids, header->n_ch);
    for (size_t i = TJIP_BLOCK_HEAD_SIZE + header->n_ch; i < offset; ++i) {
        out[i] = 0;
    }
    return offset;
}

size_t tjip_block_write_row(const float *values, uint8_t n_ch,
                            uint8_t *out, size_t out_cap)
{
    const size_t need = (size_t)n_ch * sizeof(float);
    if (out_cap < need) {
        return 0;
    }
    for (uint8_t i = 0; i < n_ch; ++i) {
        tjip_put_f32(out + (size_t)i * sizeof(float), values[i]);
    }
    return need;
}

size_t tjip_block_read_header(const uint8_t *payload, size_t len,
                              tjip_block_header_t *out_header)
{
    if (len < TJIP_BLOCK_HEAD_SIZE || payload[0] != TJIP_BLOCK_VERSION) {
        return 0;
    }
    const uint8_t n_ch = payload[1];
    if (n_ch == 0 || len < TJIP_BLOCK_HEAD_SIZE + (size_t)n_ch) {
        return 0;
    }

    out_header->n_ch        = n_ch;
    out_header->n_samples   = tjip_get_u16(payload + 2);
    out_header->first_seq   = tjip_get_u32(payload + 4);
    out_header->t0_us       = tjip_get_u64(payload + 8);
    out_header->dt_us       = tjip_get_u32(payload + 16);
    out_header->fault_mask  = tjip_get_u32(payload + 20);
    out_header->flags       = payload[24];
    out_header->seq_step    = tjip_get_u16(payload + 26);
    if (out_header->seq_step == 0) {
        out_header->seq_step = 1;
    }
    out_header->channel_ids = payload + TJIP_BLOCK_HEAD_SIZE;

    const size_t offset = tjip_block_data_offset(n_ch);
    const size_t need = offset + (size_t)out_header->n_samples * n_ch * sizeof(float);
    return len >= need ? offset : 0;
}

/* ------------------------------------------------------------------ time sync */

bool tjip_time_sync_parse(const uint8_t *payload, size_t len, uint64_t *out_t1_ns)
{
    if (len < 8u) {
        return false;
    }
    *out_t1_ns = tjip_get_u64(payload);
    return true;
}

size_t tjip_time_echo_build(uint64_t t1_host_ns, uint64_t t2_dev_us, uint64_t t3_dev_us,
                            uint8_t *out, size_t out_cap)
{
    if (out_cap < 24u) {
        return 0;
    }
    tjip_put_u64(out, t1_host_ns);
    tjip_put_u64(out + 8, t2_dev_us);
    tjip_put_u64(out + 16, t3_dev_us);
    return 24u;
}

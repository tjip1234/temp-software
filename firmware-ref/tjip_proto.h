/*
 * tjip_proto.h — reference implementation of the TJIP-1 wire protocol.
 *
 * Portable C11, no dynamic allocation, no dependencies beyond <string.h> and
 * <stdint.h>. Drop this pair of files into an ESP-IDF component (or anywhere
 * else) and it will build unchanged.
 *
 * This is the device side of what src/tjiptemp/protocol/ implements on the host,
 * and the two are tested against each other. If you change one, change both, and
 * bump the version in docs/protocol.md.
 *
 * Typical use in firmware:
 *
 *     static tjip_reader_t reader;
 *     static uint8_t rx_scratch[TJIP_MAX_FRAME];
 *     tjip_reader_init(&reader, rx_scratch, sizeof rx_scratch);
 *
 *     // in the USB/TCP receive callback:
 *     tjip_frame_t frame;
 *     size_t consumed = 0;
 *     while (tjip_reader_feed(&reader, data, len, &consumed, &frame)) {
 *         handle_request(&frame);
 *         data += consumed; len -= consumed;
 *     }
 *
 *     // to send:
 *     uint8_t out[TJIP_MAX_FRAME];
 *     size_t n = tjip_encode(TJIP_MSG_STATUS, 0, 0, json, json_len,
 *                            out, sizeof out);
 *     usb_write(out, n);
 *
 * Everything is little-endian on the wire; the helpers below do the byte packing
 * explicitly rather than memcpy-ing structs, so the code is correct on a
 * big-endian target too and does not depend on struct padding.
 */

#ifndef TJIP_PROTO_H
#define TJIP_PROTO_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TJIP_PROTOCOL_VERSION 1

/* Largest payload this build will accept or emit. Sized for the 4096-byte
 * payload advertised in DEVICE_INFO.caps.max_payload, plus header, CRC and
 * worst-case COBS overhead (1 byte per 254). */
#define TJIP_MAX_PAYLOAD 4096u
#define TJIP_HEADER_SIZE 6u
#define TJIP_CRC_SIZE    2u
#define TJIP_MAX_FRAME   (TJIP_MAX_PAYLOAD + TJIP_HEADER_SIZE + TJIP_CRC_SIZE + \
                          ((TJIP_MAX_PAYLOAD + TJIP_HEADER_SIZE + TJIP_CRC_SIZE) / 254u) + 2u)

/* ------------------------------------------------------------------ messages */

typedef enum {
    TJIP_MSG_HELLO            = 0x01,
    TJIP_MSG_DEVICE_INFO      = 0x02,
    TJIP_MSG_GET_CONFIG       = 0x10,
    TJIP_MSG_CONFIG           = 0x11,
    TJIP_MSG_SET_CONFIG       = 0x12,
    TJIP_MSG_GET_CAL          = 0x20,
    TJIP_MSG_CAL              = 0x21,
    TJIP_MSG_SET_CAL          = 0x22,
    TJIP_MSG_STREAM_START     = 0x30,
    TJIP_MSG_STREAM_STOP      = 0x31,
    TJIP_MSG_SAMPLE_BLOCK     = 0x32,
    TJIP_MSG_STATUS           = 0x33,
    TJIP_MSG_TIME_SYNC        = 0x40,
    TJIP_MSG_TIME_ECHO        = 0x41,
    TJIP_MSG_GET_RANGE        = 0x50,
    TJIP_MSG_RANGE_BLOCK      = 0x51,
    TJIP_MSG_RANGE_END        = 0x52,
    TJIP_MSG_DISPLAY_SET      = 0x60,
    TJIP_MSG_LED_SET          = 0x61,
    TJIP_MSG_IDENTIFY         = 0x62,
    TJIP_MSG_SELF_TEST        = 0x70,
    TJIP_MSG_SELF_TEST_RESULT = 0x71,
    TJIP_MSG_WIFI_PROVISION   = 0x78,
    TJIP_MSG_WIFI_STATUS      = 0x79,
    TJIP_MSG_FACTORY_RESET    = 0x7A,
    TJIP_MSG_LOG              = 0x7E,
    TJIP_MSG_ERROR            = 0x7F
} tjip_msg_t;

/* Frame flags. */
#define TJIP_FLAG_RESPONSE 0x01u
#define TJIP_FLAG_ERROR    0x02u
#define TJIP_FLAG_MORE     0x04u

/* Canonical channel ids. Do not renumber: these appear in stored recordings. */
typedef enum {
    TJIP_CH_PT1000      = 0,
    TJIP_CH_TYPEK       = 1,
    TJIP_CH_TYPEK_CJ    = 2,
    TJIP_CH_NTC_EXT1    = 3,
    TJIP_CH_NTC_EXT2    = 4,
    TJIP_CH_NTC_EXT3    = 5,
    TJIP_CH_NTC_BRD_RTD = 6,
    TJIP_CH_NTC_BRD_TC  = 7,
    TJIP_CH_NTC_BRD_CHG = 8,
    TJIP_CH_V_BAT       = 9,
    TJIP_CH_V_CC        = 10,
    TJIP_CH_AHT20_T     = 11,
    TJIP_CH_AHT20_RH    = 12,
    TJIP_CH_PT1000_R    = 13,
    TJIP_CH_TYPEK_UV    = 14,
    TJIP_CH_COUNT       = 15
} tjip_channel_t;

typedef struct {
    uint8_t        msg_type;
    uint8_t        flags;
    uint16_t       seq;
    const uint8_t *payload;   /* points into the reader's scratch buffer */
    uint16_t       payload_len;
} tjip_frame_t;

/* ----------------------------------------------------------------- checksums */

/* CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final xor.
 * Check value over "123456789" is 0x29B1. */
uint16_t tjip_crc16(const uint8_t *data, size_t len);

/* ---------------------------------------------------------------------- COBS */

/* Encode `len` bytes. Returns the encoded length, or 0 if `out_cap` is too
 * small. Requires out_cap >= len + len/254 + 2. Output never contains 0x00. */
size_t tjip_cobs_encode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap);

/* Decode one delimiter-free block. Returns the decoded length, or 0 on a
 * malformed block or insufficient capacity. */
size_t tjip_cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap);

/* -------------------------------------------------------------------- frames */

/* Build a complete on-the-wire frame (COBS + trailing 0x00 delimiter).
 * Returns the number of bytes written, or 0 if it would not fit. */
size_t tjip_encode(uint8_t msg_type, uint8_t flags, uint16_t seq,
                   const void *payload, size_t payload_len,
                   uint8_t *out, size_t out_cap);

/* Convenience wrapper for a JSON response to a request. */
size_t tjip_encode_response(uint8_t msg_type, uint16_t seq,
                            const char *json, size_t json_len,
                            uint8_t *out, size_t out_cap);

/* Convenience wrapper for an ERROR reply. `code` and `msg` must not contain
 * characters needing JSON escaping; keep codes to the documented set. */
size_t tjip_encode_error(uint16_t seq, const char *code, const char *msg,
                         uint8_t *out, size_t out_cap);

/* ------------------------------------------------------------------- reader */

typedef struct {
    uint8_t *scratch;      /* caller-owned, >= TJIP_MAX_FRAME */
    size_t   scratch_cap;
    size_t   fill;         /* bytes accumulated in the current block */
    bool     overflow;     /* current block already too long; discard to delimiter */
    uint32_t good_frames;
    uint32_t bad_frames;   /* CRC failures, malformed COBS, length mismatches */
    uint32_t dropped_bytes;
} tjip_reader_t;

void tjip_reader_init(tjip_reader_t *reader, uint8_t *scratch, size_t scratch_cap);
void tjip_reader_reset(tjip_reader_t *reader);

/* Feed bytes and extract at most one frame.
 *
 * Returns true when `out_frame` has been filled; `*consumed` then says how many
 * input bytes were used, and the caller should call again with the remainder.
 * Returns false when the whole input was absorbed without completing a frame.
 *
 * `out_frame->payload` points into `scratch` and stays valid only until the next
 * call. Copy it if you need it longer.
 *
 * Corrupt input is never an error: bad blocks are counted and skipped, and the
 * reader resynchronises on the next delimiter. On a serial link the first bytes
 * after the port opens are routinely garbage. */
bool tjip_reader_feed(tjip_reader_t *reader, const uint8_t *data, size_t len,
                      size_t *consumed, tjip_frame_t *out_frame);

/* ------------------------------------------------------------- sample blocks */

/* Header of a SAMPLE_BLOCK / RANGE_BLOCK payload, see docs/protocol.md §6.1.
 * 28 bytes on the wire, followed by n_ch channel ids, zero padding to a 4-byte
 * boundary, then n_samples * n_ch little-endian float32 in row-major order. */
#define TJIP_BLOCK_VERSION       1u
#define TJIP_BLOCK_HEAD_SIZE     28u
#define TJIP_BLOCK_FLAG_DECIMATED 0x01u

typedef struct {
    uint32_t first_seq;
    uint64_t t0_us;
    uint32_t dt_us;
    uint32_t fault_mask;   /* bit i set if channel_ids[i] faulted in this block */
    uint8_t  flags;
    uint16_t seq_step;     /* 1 unless the stream was decimated for a slow link */
    uint8_t  n_ch;
    uint16_t n_samples;
    const uint8_t *channel_ids;
} tjip_block_header_t;

/* Bytes a block of this shape will occupy, so a caller can size its buffer or
 * decide how many rows fit in max_payload. */
size_t tjip_block_size(uint8_t n_ch, uint16_t n_samples);

/* Rows that fit in `payload_budget` bytes for `n_ch` channels. */
uint16_t tjip_block_rows_for(uint8_t n_ch, size_t payload_budget);

/* Write a block header into `out`. Returns bytes written (header + ids + pad),
 * which is where the caller should start writing float32 sample data, or 0 if
 * it does not fit. */
size_t tjip_block_write_header(const tjip_block_header_t *header,
                               uint8_t *out, size_t out_cap);

/* Append one row of `n_ch` floats at `out` (which must point past the header).
 * Use NaN for a reading that is invalid; the host renders that as a gap and the
 * fault_mask tells it which sensor to blame. Returns bytes written. */
size_t tjip_block_write_row(const float *values, uint8_t n_ch,
                            uint8_t *out, size_t out_cap);

/* Parse a block header. Returns the offset at which sample data begins, or 0 if
 * the payload is malformed. Provided mainly so a firmware author can round-trip
 * test against the host. */
size_t tjip_block_read_header(const uint8_t *payload, size_t len,
                              tjip_block_header_t *out_header);

/* --------------------------------------------------------------- time sync */

/* TIME_SYNC payload: uint64 t1_host_ns. */
bool tjip_time_sync_parse(const uint8_t *payload, size_t len, uint64_t *out_t1_ns);

/* TIME_ECHO payload: t1 echoed, plus the device clock at receive and at send.
 * Read the device clock as late as possible before handing the frame to the
 * transport: everything between t3 and the byte leaving the wire shows up as
 * asymmetry, and asymmetry is the one error an SNTP exchange cannot detect. */
size_t tjip_time_echo_build(uint64_t t1_host_ns, uint64_t t2_dev_us, uint64_t t3_dev_us,
                            uint8_t *out, size_t out_cap);

/* ------------------------------------------------------- little-endian helpers */

static inline void tjip_put_u16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)(v >> 8);
}

static inline void tjip_put_u32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    p[2] = (uint8_t)((v >> 16) & 0xFF);
    p[3] = (uint8_t)((v >> 24) & 0xFF);
}

static inline void tjip_put_u64(uint8_t *p, uint64_t v) {
    for (int i = 0; i < 8; ++i) {
        p[i] = (uint8_t)((v >> (8 * i)) & 0xFF);
    }
}

static inline uint16_t tjip_get_u16(const uint8_t *p) {
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static inline uint32_t tjip_get_u32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static inline uint64_t tjip_get_u64(const uint8_t *p) {
    uint64_t v = 0;
    for (int i = 7; i >= 0; --i) {
        v = (v << 8) | p[i];
    }
    return v;
}

/* float32 is stored bit-for-bit; this avoids the strict-aliasing trap that
 * casting a float* to a uint32_t* would create. */
static inline void tjip_put_f32(uint8_t *p, float v) {
    uint32_t bits;
    __builtin_memcpy(&bits, &v, sizeof bits);
    tjip_put_u32(p, bits);
}

static inline float tjip_get_f32(const uint8_t *p) {
    uint32_t bits = tjip_get_u32(p);
    float v;
    __builtin_memcpy(&v, &bits, sizeof v);
    return v;
}

#ifdef __cplusplus
}
#endif

#endif /* TJIP_PROTO_H */

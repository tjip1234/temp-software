/*
 * Self-test and cross-check harness for the reference codec.
 *
 *     make test          run the C-only self-tests
 *     make crosscheck    also verify byte-for-byte agreement with the Python host
 *
 * The cross-check is the one that matters: it proves this C and
 * src/tjiptemp/protocol/ produce identical bytes, which is the whole point of
 * shipping a reference implementation alongside a specification.
 */

#include "tjip_proto.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures = 0;

#define CHECK(cond, ...)                                                     \
    do {                                                                     \
        if (!(cond)) {                                                       \
            printf("FAIL %s:%d  ", __FILE__, __LINE__);                      \
            printf(__VA_ARGS__);                                             \
            printf("\n");                                                    \
            failures++;                                                      \
        }                                                                    \
    } while (0)

static void print_hex(const char *label, const uint8_t *data, size_t len)
{
    printf("%s", label);
    for (size_t i = 0; i < len; ++i) {
        printf("%02x", data[i]);
    }
    printf("\n");
}

/* ------------------------------------------------------------------- tests */

static void test_crc(void)
{
    const uint8_t check[] = "123456789";
    const uint16_t crc = tjip_crc16(check, 9);
    CHECK(crc == 0x29B1, "CRC-16/CCITT-FALSE check value is 0x%04X, expected 0x29B1", crc);
}

static void test_cobs_roundtrip(void)
{
    static const struct { const char *name; size_t len; } cases[] = {
        {"empty", 0}, {"one zero", 1}, {"long zeros", 300},
        {"mixed", 512}, {"all 0xff", 600},
    };

    for (size_t c = 0; c < sizeof cases / sizeof cases[0]; ++c) {
        uint8_t in[1024];
        const size_t len = cases[c].len;
        for (size_t i = 0; i < len; ++i) {
            if (c == 1 || c == 2) {
                in[i] = 0;
            } else if (c == 4) {
                in[i] = 0xFF;
            } else {
                in[i] = (uint8_t)(i * 7u);
            }
        }

        uint8_t encoded[2048];
        uint8_t decoded[2048];
        const size_t enc = tjip_cobs_encode(in, len, encoded, sizeof encoded);
        CHECK(enc > 0, "%s: encode failed", cases[c].name);
        for (size_t i = 0; i < enc; ++i) {
            CHECK(encoded[i] != 0, "%s: encoded output contains a zero at %zu",
                  cases[c].name, i);
        }
        const size_t dec = tjip_cobs_decode(encoded, enc, decoded, sizeof decoded);
        CHECK(dec == len, "%s: decoded %zu bytes, expected %zu", cases[c].name, dec, len);
        CHECK(memcmp(in, decoded, len) == 0, "%s: roundtrip mismatch", cases[c].name);
    }
}

static void test_frame_roundtrip(void)
{
    const char *json = "{\"proto\":1,\"serial\":\"TJIP-TEST\"}";
    uint8_t wire[TJIP_MAX_FRAME];
    const size_t n = tjip_encode(TJIP_MSG_DEVICE_INFO, TJIP_FLAG_RESPONSE, 0x1234,
                                 json, strlen(json), wire, sizeof wire);
    CHECK(n > 0, "encode failed");
    CHECK(wire[n - 1] == 0x00, "frame must end with the delimiter");

    uint8_t scratch[TJIP_MAX_FRAME];
    tjip_reader_t reader;
    tjip_reader_init(&reader, scratch, sizeof scratch);

    tjip_frame_t frame;
    size_t consumed = 0;
    const bool got = tjip_reader_feed(&reader, wire, n, &consumed, &frame);
    CHECK(got, "reader did not produce a frame");
    if (got) {
        CHECK(frame.msg_type == TJIP_MSG_DEVICE_INFO, "wrong msg_type 0x%02X", frame.msg_type);
        CHECK(frame.seq == 0x1234, "wrong seq %u", frame.seq);
        CHECK(frame.flags == TJIP_FLAG_RESPONSE, "wrong flags 0x%02X", frame.flags);
        CHECK(frame.payload_len == strlen(json), "wrong payload length %u", frame.payload_len);
        CHECK(memcmp(frame.payload, json, frame.payload_len) == 0, "payload mismatch");
    }
}

static void test_reader_resyncs_after_corruption(void)
{
    uint8_t wire[TJIP_MAX_FRAME * 3];
    size_t total = 0;

    /* garbage, then a good frame, then more garbage, then another good frame */
    const uint8_t junk[] = {0x99, 0x88, 0x77, 0x00};
    memcpy(wire + total, junk, sizeof junk);
    total += sizeof junk;
    total += tjip_encode(TJIP_MSG_HELLO, 0, 1, "one", 3, wire + total, sizeof wire - total);
    memcpy(wire + total, junk, sizeof junk);
    total += sizeof junk;
    total += tjip_encode(TJIP_MSG_HELLO, 0, 2, "two", 3, wire + total, sizeof wire - total);

    uint8_t scratch[TJIP_MAX_FRAME];
    tjip_reader_t reader;
    tjip_reader_init(&reader, scratch, sizeof scratch);

    const uint8_t *cursor = wire;
    size_t remaining = total;
    int found = 0;
    uint16_t seqs[4] = {0};
    tjip_frame_t frame;
    size_t consumed = 0;
    while (tjip_reader_feed(&reader, cursor, remaining, &consumed, &frame)) {
        if (found < 4) {
            seqs[found] = frame.seq;
        }
        found++;
        cursor += consumed;
        remaining -= consumed;
    }
    CHECK(found == 2, "expected 2 frames past the corruption, got %d", found);
    CHECK(seqs[0] == 1 && seqs[1] == 2, "frames arrived out of order");
    CHECK(reader.bad_frames == 2, "expected 2 bad blocks, counted %u", reader.bad_frames);
}

static void test_byte_at_a_time(void)
{
    /* A USB CDC read returns whatever happened to be in the FIFO. */
    uint8_t wire[TJIP_MAX_FRAME];
    const size_t n = tjip_encode(TJIP_MSG_STATUS, 0, 0, "{\"ok\":true}", 11,
                                 wire, sizeof wire);
    uint8_t scratch[TJIP_MAX_FRAME];
    tjip_reader_t reader;
    tjip_reader_init(&reader, scratch, sizeof scratch);

    int found = 0;
    for (size_t i = 0; i < n; ++i) {
        tjip_frame_t frame;
        size_t consumed = 0;
        if (tjip_reader_feed(&reader, wire + i, 1, &consumed, &frame)) {
            found++;
            CHECK(frame.msg_type == TJIP_MSG_STATUS, "wrong type in dribbled frame");
        }
    }
    CHECK(found == 1, "byte-at-a-time delivery produced %d frames, expected 1", found);
}

static void test_sample_block(void)
{
    const uint8_t ids[] = {TJIP_CH_PT1000, TJIP_CH_TYPEK, TJIP_CH_V_BAT};
    const uint16_t rows = 5;

    tjip_block_header_t header = {
        .first_seq = 1000, .t0_us = 123456789ull, .dt_us = 10000,
        .fault_mask = 0x2, .flags = 0, .seq_step = 1,
        .n_ch = 3, .n_samples = rows, .channel_ids = ids,
    };

    uint8_t payload[512];
    size_t offset = tjip_block_write_header(&header, payload, sizeof payload);
    CHECK(offset == 32u, "header+ids+pad should be 32 bytes for 3 channels, got %zu", offset);
    CHECK(offset % 4u == 0, "sample data must start 4-byte aligned");

    for (uint16_t r = 0; r < rows; ++r) {
        const float values[3] = {21.0f + r, (r == 2) ? NAN : 25.0f + r, 3.9f};
        offset += tjip_block_write_row(values, 3, payload + offset, sizeof payload - offset);
    }
    CHECK(offset == tjip_block_size(3, rows), "block size mismatch: %zu vs %zu",
          offset, tjip_block_size(3, rows));

    tjip_block_header_t parsed;
    const size_t data_at = tjip_block_read_header(payload, offset, &parsed);
    CHECK(data_at == 32u, "parsed data offset %zu", data_at);
    CHECK(parsed.first_seq == 1000 && parsed.n_samples == rows, "header fields mismatch");
    CHECK(parsed.t0_us == 123456789ull, "t0_us mismatch");
    CHECK(parsed.seq_step == 1, "seq_step should default to 1");

    const float second = tjip_get_f32(payload + data_at + (2 * 3 + 1) * 4);
    CHECK(isnan(second), "a NaN reading must survive the roundtrip");

    /* Decimated blocks must set the flag, so the host does not chase phantom gaps. */
    header.seq_step = 5;
    tjip_block_write_header(&header, payload, sizeof payload);
    tjip_block_read_header(payload, sizeof payload, &parsed);
    CHECK(parsed.flags & TJIP_BLOCK_FLAG_DECIMATED,
          "seq_step != 1 must imply the DECIMATED flag");
}

static void test_rows_for_budget(void)
{
    /* 15 channels in a 4096-byte payload: header is 28+15 padded to 44. */
    const uint16_t rows = tjip_block_rows_for(15, 4096);
    CHECK(rows == (4096 - 44) / 60, "rows_for(15, 4096) = %u", rows);
    CHECK(tjip_block_size(15, rows) <= 4096, "computed rows do not fit the budget");
    CHECK(tjip_block_rows_for(0, 4096) == 0, "zero channels must yield zero rows");
}

static void test_time_sync(void)
{
    uint8_t payload[24];
    const size_t n = tjip_time_echo_build(0x1122334455667788ull, 111, 222,
                                          payload, sizeof payload);
    CHECK(n == 24, "TIME_ECHO payload is %zu bytes, expected 24", n);
    CHECK(tjip_get_u64(payload) == 0x1122334455667788ull, "t1 not echoed correctly");
    CHECK(tjip_get_u64(payload + 8) == 111, "t2 mismatch");

    uint64_t t1 = 0;
    CHECK(tjip_time_sync_parse(payload, 8, &t1), "parse failed");
    CHECK(t1 == 0x1122334455667788ull, "parsed t1 mismatch");
    CHECK(!tjip_time_sync_parse(payload, 4, &t1), "a short payload must be rejected");
}

/* -------------------------------------------------------------- cross-check */

/* Emit the exact bytes the Python side is asked to produce, so the two can be
 * diffed. Run with: ./test_tjip_proto --vectors */
static void emit_vectors(void)
{
    uint8_t wire[TJIP_MAX_FRAME];
    size_t n;

    n = tjip_encode(TJIP_MSG_HELLO, 0, 1, "{\"proto\":1}", 11, wire, sizeof wire);
    print_hex("hello ", wire, n);

    n = tjip_encode(TJIP_MSG_STATUS, 0, 0, "", 0, wire, sizeof wire);
    print_hex("empty ", wire, n);

    uint8_t payload[300];
    for (size_t i = 0; i < sizeof payload; ++i) {
        payload[i] = (uint8_t)(i * 7u);
    }
    n = tjip_encode(TJIP_MSG_CONFIG, TJIP_FLAG_RESPONSE | TJIP_FLAG_MORE, 0xBEEF,
                    payload, sizeof payload, wire, sizeof wire);
    print_hex("binary ", wire, n);

    const uint8_t ids[] = {0, 1, 9};
    tjip_block_header_t header = {
        .first_seq = 1000, .t0_us = 123456789ull, .dt_us = 10000,
        .fault_mask = 2, .flags = 0, .seq_step = 1,
        .n_ch = 3, .n_samples = 4, .channel_ids = ids,
    };
    uint8_t block[512];
    size_t offset = tjip_block_write_header(&header, block, sizeof block);
    for (uint16_t r = 0; r < 4; ++r) {
        const float values[3] = {21.5f + r, 25.25f + r, 3.9f};
        offset += tjip_block_write_row(values, 3, block + offset, sizeof block - offset);
    }
    print_hex("block ", block, offset);

    n = tjip_time_echo_build(0x1122334455667788ull, 111, 222, wire, sizeof wire);
    print_hex("timeecho ", wire, n);
}

int main(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "--vectors") == 0) {
        emit_vectors();
        return 0;
    }

    test_crc();
    test_cobs_roundtrip();
    test_frame_roundtrip();
    test_reader_resyncs_after_corruption();
    test_byte_at_a_time();
    test_sample_block();
    test_rows_for_budget();
    test_time_sync();

    if (failures == 0) {
        printf("all reference codec tests passed\n");
        return 0;
    }
    printf("%d check(s) failed\n", failures);
    return 1;
}

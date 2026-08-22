CC      ?= cc
CFLAGS  ?= -std=c11 -Wall -Wextra -Wpedantic -Wconversion -Wshadow -O2 -g
LDLIBS  += -lm

.PHONY: all test crosscheck clean

all: test_tjip_proto

test_tjip_proto: test_tjip_proto.c tjip_proto.c tjip_proto.h
	$(CC) $(CFLAGS) -o $@ test_tjip_proto.c tjip_proto.c $(LDLIBS)

test: test_tjip_proto
	./test_tjip_proto

# Prove this C and the Python host produce identical bytes. This is the check
# that keeps the specification honest.
crosscheck: test_tjip_proto
	./test_tjip_proto --vectors > /tmp/tjip-c-vectors.txt
	cd .. && python3 -m tests.crosscheck_vectors > /tmp/tjip-py-vectors.txt
	diff -u /tmp/tjip-c-vectors.txt /tmp/tjip-py-vectors.txt && \
		echo "C and Python codecs agree byte-for-byte"

clean:
	rm -f test_tjip_proto

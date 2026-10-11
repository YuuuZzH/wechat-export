/*
 * Standalone SILK V3 decoder — reads .silk file, writes raw 16-bit PCM.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* SILK SDK headers */
#include "SKP_Silk_typedef.h"
#include "SKP_Silk_errors.h"
#include "SKP_Silk_control.h"
#include "SKP_Silk_SDK_API.h"

/* Assert stub required by SILK SDK */
void SKP_assert(int x) { (void)x; }

#define DECODE_MAX_BYTES_PER_FRAME  1024
#define MAX_INPUT_FRAMES            5
#define MAX_LBRR_DELAY              2

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: silk_decoder <input.silk> [output.pcm] [sample_rate]\n");
        return 1;
    }

    const char *in_path = argv[1];
    const char *out_path = argc > 2 ? argv[2] : NULL;
    int API_Fs_Hz = argc > 3 ? atoi(argv[3]) : 24000;

    FILE *fin = fopen(in_path, "rb");
    if (!fin) { fprintf(stderr, "Cannot open input: %s\n", in_path); return 1; }

    /* Validate SILK V3 header */
    {
        char hdr[10];
        size_t n = fread(hdr, 1, 10, fin);
        if (n < 10 || hdr[0] != '\x02' || strncmp(hdr + 1, "#!SILK_V3", 9) != 0) {
            fprintf(stderr, "Not a valid SILK V3 file\n");
            fclose(fin);
            return 1;
        }
    }

    FILE *fout = out_path ? fopen(out_path, "wb") : stdout;
    if (!fout) { fprintf(stderr, "Cannot open output: %s\n", out_path); fclose(fin); return 1; }

    SKP_SILK_SDK_DecControlStruct DecControl;
    memset(&DecControl, 0, sizeof(DecControl));
    DecControl.API_sampleRate = API_Fs_Hz;
    DecControl.framesPerPacket = 1;

    SKP_int32 decSizeBytes;
    SKP_Silk_SDK_Get_Decoder_Size(&decSizeBytes);
    void *psDec = malloc(decSizeBytes);
    if (!psDec) { fprintf(stderr, "Out of memory\n"); return 1; }
    SKP_Silk_SDK_InitDecoder(psDec);

    SKP_uint8 payload[DECODE_MAX_BYTES_PER_FRAME * MAX_INPUT_FRAMES * (MAX_LBRR_DELAY + 1)];
    SKP_uint8 *payloadEnd = payload;
    SKP_uint8 FECpayload[DECODE_MAX_BYTES_PER_FRAME * MAX_INPUT_FRAMES];
    SKP_int16 nBytesPerPacket[MAX_LBRR_DELAY + 1];
    SKP_int16 out[((20 * 48) << 1) * MAX_INPUT_FRAMES];
    int totPackets = 0;

    /* Fill jitter buffer with first MAX_LBRR_DELAY packets */
    for (int i = 0; i < MAX_LBRR_DELAY; i++) {
        SKP_int16 nBytes;
        if (fread(&nBytes, sizeof(SKP_int16), 1, fin) != 1) break;
        if (nBytes <= 0 || nBytes > DECODE_MAX_BYTES_PER_FRAME) break;
        if (fread(payloadEnd, 1, nBytes, fin) != (size_t)nBytes) break;
        nBytesPerPacket[i] = nBytes;
        payloadEnd += nBytes;
        totPackets++;
    }

    while (1) {
        SKP_int16 nBytes;
        if (fread(&nBytes, sizeof(SKP_int16), 1, fin) != 1) break;
        if (nBytes <= 0 || nBytes > DECODE_MAX_BYTES_PER_FRAME) break;
        if (fread(payloadEnd, 1, nBytes, fin) != (size_t)nBytes) break;

        nBytesPerPacket[MAX_LBRR_DELAY] = nBytes;
        payloadEnd += nBytes;

        SKP_int16 nBytesOut, *outPtr;
        SKP_int16 tot_len;
        SKP_uint8 *payloadToDec;
        int lost;

        if (nBytesPerPacket[0] == 0) {
            lost = 1;
            SKP_uint8 *payloadPtr = payload;
            for (int i = 0; i < MAX_LBRR_DELAY; i++) {
                if (nBytesPerPacket[i + 1] > 0) {
                    SKP_int16 nBytesFEC;
                    SKP_Silk_SDK_search_for_LBRR(payloadPtr, nBytesPerPacket[i + 1], i + 1, FECpayload, &nBytesFEC);
                    if (nBytesFEC > 0) {
                        payloadToDec = FECpayload;
                        nBytesOut = nBytesFEC;
                        lost = 0;
                        break;
                    }
                }
                payloadPtr += nBytesPerPacket[i + 1];
            }
        } else {
            lost = 0;
            nBytesOut = nBytesPerPacket[0];
            payloadToDec = payload;
        }

        outPtr = out;
        tot_len = 0;

        if (!lost) {
            do {
                SKP_int16 len;
                SKP_Silk_SDK_Decode(psDec, &DecControl, 0, payloadToDec, nBytesOut, outPtr, &len);
                outPtr += len;
                tot_len += len;
            } while (DecControl.moreInternalDecoderFrames);
        } else {
            for (int i = 0; i < DecControl.framesPerPacket; i++) {
                SKP_int16 len;
                SKP_Silk_SDK_Decode(psDec, &DecControl, 1, payloadToDec, nBytesOut, outPtr, &len);
                outPtr += len;
                tot_len += len;
            }
        }

        fwrite(out, sizeof(SKP_int16), tot_len, fout);
        totPackets++;

        /* Slide buffer */
        int totBytes = 0;
        for (int i = 0; i < MAX_LBRR_DELAY; i++)
            totBytes += nBytesPerPacket[i + 1];
        memmove(payload, &payload[nBytesPerPacket[0]], totBytes * sizeof(SKP_uint8));
        payloadEnd -= nBytesPerPacket[0];
        memmove(nBytesPerPacket, &nBytesPerPacket[1], MAX_LBRR_DELAY * sizeof(SKP_int16));
    }

    /* Drain remaining packets */
    for (int k = 0; k < MAX_LBRR_DELAY; k++) {
        if (nBytesPerPacket[0] == 0) break;

        SKP_int16 nBytesOut, *outPtr, tot_len;
        SKP_uint8 *payloadToDec;
        int lost;

        if (nBytesPerPacket[0] == 0) {
            lost = 1;
            SKP_uint8 *payloadPtr = payload;
            for (int i = 0; i < MAX_LBRR_DELAY; i++) {
                if (nBytesPerPacket[i + 1] > 0) {
                    SKP_int16 nBytesFEC;
                    SKP_Silk_SDK_search_for_LBRR(payloadPtr, nBytesPerPacket[i + 1], i + 1, FECpayload, &nBytesFEC);
                    if (nBytesFEC > 0) {
                        payloadToDec = FECpayload;
                        nBytesOut = nBytesFEC;
                        lost = 0;
                        break;
                    }
                }
                payloadPtr += nBytesPerPacket[i + 1];
            }
        } else {
            lost = 0;
            nBytesOut = nBytesPerPacket[0];
            payloadToDec = payload;
        }

        outPtr = out;
        tot_len = 0;

        if (!lost) {
            do {
                SKP_int16 len;
                SKP_Silk_SDK_Decode(psDec, &DecControl, 0, payloadToDec, nBytesOut, outPtr, &len);
                outPtr += len;
                tot_len += len;
            } while (DecControl.moreInternalDecoderFrames);
        } else {
            for (int i = 0; i < DecControl.framesPerPacket; i++) {
                SKP_int16 len;
                SKP_Silk_SDK_Decode(psDec, &DecControl, 1, payloadToDec, nBytesOut, outPtr, &len);
                outPtr += len;
                tot_len += len;
            }
        }

        fwrite(out, sizeof(SKP_int16), tot_len, fout);

        int totBytes = 0;
        for (int i = 0; i < MAX_LBRR_DELAY; i++)
            totBytes += nBytesPerPacket[i + 1];
        memmove(payload, &payload[nBytesPerPacket[0]], totBytes * sizeof(SKP_uint8));
        payloadEnd -= nBytesPerPacket[0];
        memmove(nBytesPerPacket, &nBytesPerPacket[1], MAX_LBRR_DELAY * sizeof(SKP_int16));
    }

    free(psDec);
    if (out_path) fclose(fout);
    fclose(fin);
    return 0;
}

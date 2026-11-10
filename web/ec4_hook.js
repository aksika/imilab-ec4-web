/* ec4_hook.js — tap the Alibaba Link Visual decoder and stream encoded H.265 NALs.
 *
 * Native-only: no Java bridge needed.  Two functions in liblvmedia.so are hooked:
 *   ffmpegVideoDecoder::SetExtraData  (0x1e9fd4) -> Annex-B VPS/SPS/PPS
 *   ffmpegVideoDecoder::decodeVideo   (0x1e9a38) -> AVCC H.265 NALs
 *
 * Messages:
 *   {type:'ready'}
 *   {type:'extra', inst, w, h}   + binary Annex-B parameter sets
 *   {type:'nal',   inst, key}    + binary H.265 NAL (without start code)
 */
(function () {
    var base = Process.getModuleByName('liblvmedia.so').base;
    var logged = {};

    function sendBytes(msg, bytes) {
        try {
            var ab = new ArrayBuffer(bytes.length);
            new Uint8Array(ab).set(bytes);
            send(msg, ab);
        } catch (e) {
            console.log('[ec4] send error: ' + e);
        }
    }

    Interceptor.attach(base.add(0x1e9fd4), {
        onEnter: function (args) {
            try {
                var data = args[1];
                var size = args[2].toInt32();
                if (size <= 0 || data.isNull()) return;
                var inst = args[0].toString();
                var w = args[3].toInt32();
                var h = args[4].toInt32();
                var bytes = new Uint8Array(data.readByteArray(size));
                send({ type: 'extra', inst: inst, w: w, h: h }, bytes.buffer);
                if (!logged['e' + inst]) {
                    logged['e' + inst] = true;
                    console.log('[ec4] extra ' + w + 'x' + h + ' ' + size + 'B inst=' + inst);
                }
            } catch (e) { }
        }
    });

    Interceptor.attach(base.add(0x1e9a38), {
        onEnter: function (args) {
            try {
                var info = args[1];
                if (info.isNull()) return;
                var data = info.readPointer();
                var size = info.add(8).readS32();
                if (size <= 0 || data.isNull()) return;
                var u8 = new Uint8Array(data.readByteArray(size));
                var inst = args[0].toString();
                var off = 0;
                while (off + 4 <= u8.length) {
                    var len = ((u8[off] << 24) | (u8[off + 1] << 16) | (u8[off + 2] << 8) | u8[off + 3]) >>> 0;
                    if (len <= 0 || off + 4 + len > u8.length) break;
                    var nal = u8.subarray(off + 4, off + 4 + len);
                    if (nal.length > 0) {
                        var type = (nal[0] >> 1) & 0x3f;
                        var key = (type >= 16 && type <= 21);
                        sendBytes({ type: 'nal', inst: inst, key: key, nalType: type }, nal);
                    }
                    off += 4 + len;
                }
            } catch (e) { }
        }
    });

    send({ type: 'ready' });
})();

// flash.py runs this from its qdl.js checkout: bun up_flash.js <programmer> <partition>=<image>...
// flashes each image to its partition (e.g. system_a, userdata) in one firehose session, then makes slot a active and resets
import { webusb } from "usb";
import { qdlDevice } from "./src/qdl.js";
import { usbClass } from "./src/usblib.js";

const [programmer, ...images] = process.argv.slice(2);
navigator.usb = webusb;
const qdl = new qdlDevice(await Bun.file(programmer).arrayBuffer());
await qdl.connect(new usbClass());

for (const arg of images) {
  const [partition, path] = arg.split("=");
  const start = performance.now();
  // a sparse image is flashed onto an erased partition, but erasing all of userdata takes longer than firehose waits, and
  // a fresh ext4 never reads the blocks it leaves out
  if (!await qdl.flashBlob(partition, Bun.file(path), undefined, partition !== "userdata")) throw new Error(`flashing ${partition} failed`);
  console.info(`flashed ${partition} in ${((performance.now() - start) / 1000).toFixed(1)}s`);
}
if (!await qdl.setActiveSlot("a")) throw new Error("setting slot a active failed");
await qdl.reset();
process.exit(0);

# PhotoRoom - Apple Photos to Lightroom converter

Recreate the look of your Apple Photos edits in **Lightroom Classic**, while
keeping your originals editable. PhotoRoom matches temperature, tint, exposure,
highlights, shadows, contrast, whites, blacks, crop, and rotation. Other Develop
settings stay unchanged. Matches are approximate, so review the results.

<img width="2336" height="1045" alt="photoroom2" src="https://github.com/user-attachments/assets/9321a1af-2b0e-434a-a950-a8fa62930697" />

## 1. Export your photos

In Apple Photos, select the photos you want to migrate and export them twice:

| Export | Settings | Destination |
| --- | --- | --- |
| **File > Export > Export Unmodified Original** | Use original filenames | `orig` folder |
| **File > Export > Export Photos** | Original filenames, TIFF, 16-bit, Full Size, Display P3, embedded color profile | `edit` folder |

Names and subfolders must match apart from the file extension:

```text
orig/IMG_001.ARW
edit/IMG_001.tiff
```

**Export IPTC as XMP** is optional. Enable it to preserve metadata and keywords
added in Apple Photos; leave it off to avoid extra sidecars in `orig`.
PhotoRoom ignores these sidecars and does not import their metadata.

sRGB also works if Display P3 is unavailable. See [Apple’s export instructions](https://support.apple.com/guide/photos/pht6e157c5f/mac)
for help. PhotoRoom matches edits; it does not migrate albums or videos.

## 2. Set up once

Requires **macOS**, **Lightroom Classic 12+**, and **Python 3.10+**.

Open **Terminal** using Spotlight (**Command–Space**, type **Terminal**, press
Return). If you don’t have Homebrew, follow the installation instructions at
[brew.sh](https://brew.sh), including the installer’s **Next steps** commands.
Then install the tools PhotoRoom needs:

```bash
brew install python little-cms2
```

Skip that command if both are already installed. PhotoRoom sets up its remaining
dependencies automatically.

In Lightroom:

1. Import the photos in `orig` using **Add**, so they stay in that folder.
2. Open **File > Plug-in Manager**, click **Add**, and select
   **PhotoRoom.lrplugin** from the PhotoRoom project folder.
3. Leave the plug-in **Enabled**. It connects automatically when Python runs.

To prevent Lightroom from writing sidecars or metadata into your originals,
turn off **Catalog Settings > Metadata > Automatically write changes into XMP**.
The **Include Develop settings in metadata** checkbox is not required.

## 3. Try one photo, then run the batch

In Terminal, open your PhotoRoom folder. Replace the example paths with yours:

```bash
cd "/path/to/PhotoRoom"
```

**Check one filename pairing without making edits:**

```bash
python3 photoroom.py --orig "/path/to/orig" --edit "/path/to/edit" --dry-run --limit 1
```

**Match one photo:** keep Lightroom open with the plug-in enabled.

```bash
python3 photoroom.py --orig "/path/to/orig" --edit "/path/to/edit" --limit 1
```

Review the photo in Lightroom and the comparison preview saved in `edit`.
When you’re happy with the workflow, **run the full batch:**

```bash
python3 photoroom.py --orig "/path/to/orig" --edit "/path/to/edit"
```

Completed photos are skipped. Progress shows elapsed time and estimated time
remaining. Avoid manual edits in Lightroom while matching runs.

## 4. Review and finish

**Results are already applied in Lightroom.** Do not use **Read Metadata from
File** to load them.

Beside each edited TIFF, PhotoRoom saves a **`.match-preview.jpg`** comparison
and an **`.xmp`** record of the matched controls. The XMP marks the photo as
completed; it is not a full backup of your Lightroom edits.

The final summary repeats any warnings and errors:

- **SAVED:** completed.
- **WARNING:** best available match saved; review it.
- **FAILED:** did not complete; check the error message.

**To retry one photo**, delete its `.xmp` from `edit` and rerun the command.
Add **`--overwrite`** to redo the whole batch. Add **`--virtual-copies`** to work
on Lightroom virtual copies, or **`--prefer raw`** when RAW and JPEG originals
share a filename.

Matching stops when Python finishes. To interrupt a run, press **Ctrl+C** in
Terminal. **After migration, disable PhotoRoom in File > Plug-in Manager.**

## Listener controls

Normally, no manual controls are needed. Under **Library > Plug-in Extras**:

- **PhotoRoom: show listener status** displays its current state.
- **PhotoRoom: start listener** starts it if needed and shows feedback.
- **PhotoRoom: stop listener** requests a stop after the current operation.

After manually stopping it, choose **start listener** or disable and re-enable
the plug-in before running Python again.

Free for personal, noncommercial use only. Business or professional use requires
written permission. See [LICENSE](LICENSE).

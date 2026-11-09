# drawing

Generated from the config, never edit by hand. Regenerate (inside `nix develop`, from the repo root):

```
python -m kb.drawing
```

Output is deterministic; a test asserts these files equal a fresh run.

| file | shows |
|---|---|
| `left.svg`, `right.svg` | top view of one half: outline, keycaps, plate cutouts, key ids, encoder opening and shaft, USB-C sockets with plug ghost and aim, screw holes and heads, part courtyards with references |
| `desk.svg` | both halves as they sit on the desk: tilt, gap between halves, aim of all 4 ports, straight-up reference through each link port |
| `ports.svg` | close-ups of the computer and link ports with the decided and measured dimensions |
| `stack.svg` | side cut of the layer stack to scale: plate, PCB, bottom parts, stem tube, bottom cover, plug |
| `schematic/left.svg`, `schematic/right.svg` | KiCad root schematic of each half |
| `schematic/left-core.svg`, `schematic/right-core.svg` | KiCad core sub-sheet of each half |
| `pcb/left.svg`, `pcb/right.svg` | KiCad board seen from the bottom (mirrored, where the parts are): Edge.Cuts, B.Cu, B.Courtyard, B.Fab |

KiCad exports are normalised: the date in the title is dropped, the board export gets a white background.

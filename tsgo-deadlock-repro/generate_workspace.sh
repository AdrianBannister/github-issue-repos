#!/usr/bin/env bash
set -euo pipefail

WS="${1:-/tmp/tsgo-repro-ws}"
COUNT="${COUNT:-150}"

rm -rf "$WS"
mkdir -p "$WS/src"

cat > "$WS/tsconfig.json" <<'EOF'
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "noEmit": true
  },
  "include": ["src/**/*.ts"]
}
EOF

cat > "$WS/src/util.ts" <<'EOF'
export function add(a: number, b: number): number { return a + b; }
export function mul(a: number, b: number): number { return a * b; }
export interface Box<T> { value: T; }
export function wrap<T>(value: T): Box<T> { return { value }; }
EOF

i=0
while [ "$i" -lt "$COUNT" ]; do
    nnn=$(printf "%03d" "$i")
    next=$(printf "%03d" $(( (i + 1) % COUNT )))
    far=$(printf "%03d" $(( (i + 7) % COUNT )))
    cat > "$WS/src/file_${nnn}.ts" <<EOF
import { add, mul, wrap, Box } from "./util.js";
import { compute_${next} } from "./file_${next}.js";
import { compute_${far} } from "./file_${far}.js";

export function compute_${nnn}(x: number): Box<number> {
    const a = add(x, ${i});
    const b = mul(a, 2);
    if (x > 1000000) {
        // unreachable in practice, just keeps the import graph live
        const _y = compute_${next}(0).value + compute_${far}(0).value;
        return wrap(b + _y);
    }
    return wrap(b);
}

export const tag_${nnn} = "file_${nnn}" as const;
EOF
    i=$(( i + 1 ))
done

echo "Generated $COUNT files in $WS/src"
ls "$WS/src" | head -5
echo "..."
ls "$WS/src" | tail -3

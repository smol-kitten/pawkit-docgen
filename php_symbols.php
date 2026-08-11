<?php
/**
 * php_symbols.php — accurate PHP symbol extractor using the native tokenizer.
 *
 * Emits one JSON object per LINE (JSONL) for each function/method declaration:
 *   {file,line,kind,class,name,signature,doc(bool),doc_summary,lang}
 *
 * Used by symbols.py (no AI). Far more reliable than regex for PHP: it tracks
 * namespace/class scope, associates the preceding /** docblock, and captures the
 * real parameter list (types + defaults) straight from the token stream.
 *
 * Usage: php php_symbols.php <file.php> [more.php ...]
 */

function first_doc_line(string $doc): string {
    // strip /** */ and leading * , return the first non-empty, non-tag line
    $lines = preg_split('/\r?\n/', $doc);
    foreach ($lines as $l) {
        $l = trim($l);
        $l = preg_replace('#^/\*\*?#', '', $l);
        $l = preg_replace('#\*/$#', '', $l);
        $l = ltrim($l, "* \t");
        $l = trim($l);
        if ($l === '' || $l[0] === '@') continue;
        return mb_substr($l, 0, 160);
    }
    return '';
}

// modifier tokens that may sit between a docblock and `function` without breaking
// the association (public/private/static/abstract/final/readonly + attributes).
$MODIFIERS = [T_PUBLIC, T_PRIVATE, T_PROTECTED, T_STATIC, T_ABSTRACT, T_FINAL];
if (defined('T_READONLY')) $MODIFIERS[] = T_READONLY;
if (defined('T_ATTRIBUTE')) $MODIFIERS[] = T_ATTRIBUTE;
$MODIFIERS = array_flip($MODIFIERS);

foreach (array_slice($argv, 1) as $path) {
    $src = @file_get_contents($path);
    if ($src === false) continue;
    $toks = @token_get_all($src);
    if (!$toks) continue;
    $n = count($toks);
    $ns = '';
    $class = '';
    $pendingDoc = null;     // most recent docblock awaiting a declaration
    $vis = '';              // visibility modifier awaiting a declaration
    $prevSig = null;        // previous meaningful token id (to skip ::class)

    for ($i = 0; $i < $n; $i++) {
        $t = $toks[$i];

        if (is_string($t)) {
            // structural punctuation: '{' '}' ';' end a declaration context → drop doc
            if ($t === ';' || $t === '{' || $t === '}') { $pendingDoc = null; $vis = ''; }
            $prevSig = $t;
            continue;
        }

        [$id, $text, $line] = $t;
        if ($id === T_WHITESPACE || $id === T_COMMENT) continue;
        if ($id === T_DOC_COMMENT) { $pendingDoc = $text; $prevSig = $id; continue; }
        if ($id === T_PUBLIC) { $vis = 'public'; $prevSig = $id; continue; }
        if ($id === T_PRIVATE) { $vis = 'private'; $prevSig = $id; continue; }
        if ($id === T_PROTECTED) { $vis = 'protected'; $prevSig = $id; continue; }

        if ($id === T_NAMESPACE) {
            $parts = [];
            for ($j = $i + 1; $j < $n; $j++) {
                $tj = $toks[$j];
                if (is_string($tj)) { if ($tj === ';' || $tj === '{') break; else continue; }
                if ($tj[0] === T_WHITESPACE) continue;
                if (in_array($tj[0], [T_STRING, T_NS_SEPARATOR]) || (defined('T_NAME_QUALIFIED') && $tj[0] === T_NAME_QUALIFIED)) $parts[] = $tj[1];
                else break;
            }
            $ns = implode('', $parts);
            $pendingDoc = null; $prevSig = $id; continue;
        }

        if (in_array($id, [T_CLASS, T_INTERFACE, T_TRAIT]) || (defined('T_ENUM') && $id === T_ENUM)) {
            // skip `Foo::class` (preceded by ::) and anonymous `new class`
            if ($prevSig === T_DOUBLE_COLON) { $prevSig = $id; continue; }
            $nm = '';
            for ($j = $i + 1; $j < $n; $j++) {
                $tj = $toks[$j];
                if (is_string($tj)) break;
                if ($tj[0] === T_WHITESPACE) continue;
                if ($tj[0] === T_STRING) { $nm = $tj[1]; break; }
                break;
            }
            if ($nm !== '') {
                $class = $nm;   // new top-level class scope (approx: 1 class/file common)
                echo json_encode([
                    'file' => $path, 'line' => $line, 'kind' => 'class', 'class' => '',
                    'name' => $nm, 'signature' => '',
                    'doc' => $pendingDoc !== null,
                    'doc_summary' => $pendingDoc !== null ? first_doc_line($pendingDoc) : '',
                    'lang' => 'PHP',
                ], JSON_UNESCAPED_SLASHES) . "\n";
            }
            $pendingDoc = null; $prevSig = $id; continue;
        }

        if ($id === T_FUNCTION) {
            // function NAME ( ... )
            $name = '';
            $k = $i + 1;
            for (; $k < $n; $k++) {
                $tk = $toks[$k];
                if (is_string($tk)) { if ($tk === '&') continue; break; } // &-return
                if ($tk[0] === T_WHITESPACE) continue;
                if ($tk[0] === T_STRING) { $name = $tk[1]; break; }
                break;
            }
            if ($name === '') { $pendingDoc = null; $prevSig = $id; continue; } // closure/arrow
            // capture params: first '(' to matching ')'. Alongside the signature,
            // collect each top-level parameter's variable name so we can later flag
            // params that are declared but never referenced in the body (a hint at
            // partial implementation, a stale signature, or a plain mistake).
            $sig = ''; $depth = 0; $started = false;
            $paramVars = [];                 // ordered param names ($x) eligible for the unused check
            $expectName = true;              // at the start of each top-level param the next T_VARIABLE is its name
            $promoted = false;               // constructor property promotion (public/… $x) → used as a property
            $sawDefault = false;             // past '=' for this param → later vars belong to the default expr
            for (; $k < $n; $k++) {
                $tk = $toks[$k];
                $piece = is_string($tk) ? $tk : $tk[1];
                if ($piece === '(') { $depth++; $started = true; }
                if ($started) $sig .= $piece;
                if ($started && $depth === 1) {
                    if ($piece === ',') { $expectName = true; $promoted = false; $sawDefault = false; }
                    elseif ($piece === '=') { $sawDefault = true; }
                    elseif (!is_string($tk)) {
                        if (in_array($tk[0], [T_PUBLIC, T_PRIVATE, T_PROTECTED], true)
                            || (defined('T_READONLY') && $tk[0] === T_READONLY)) {
                            $promoted = true;
                        } elseif ($tk[0] === T_VARIABLE && $expectName && !$sawDefault) {
                            if (!$promoted && $tk[1] !== '$this') $paramVars[] = $tk[1];
                            $expectName = false;
                        }
                    }
                }
                if ($piece === ')') { $depth--; if ($depth === 0) break; }
            }
            $sig = preg_replace('/\s+/', ' ', trim($sig));

            // Determine which params go unreferenced in the body (only for real bodies;
            // abstract/interface methods end in ';' and are skipped).
            $unused = [];
            if ($paramVars) {
                $b = $k + 1; $hasBody = false;
                for (; $b < $n; $b++) {
                    $bp = is_string($toks[$b]) ? $toks[$b] : $toks[$b][1];
                    if ($bp === ';') break;              // no body
                    if ($bp === '{') { $hasBody = true; break; }
                }
                if ($hasBody) {
                    $used = []; $bd = 0; $dynamic = false;
                    for (; $b < $n; $b++) {
                        $bt = $toks[$b];
                        if (is_string($bt)) {
                            if ($bt === '{') $bd++;
                            elseif ($bt === '}') { $bd--; if ($bd === 0) break; }
                        } elseif ($bt[0] === T_CURLY_OPEN || $bt[0] === T_DOLLAR_OPEN_CURLY_BRACES) {
                            // "{$var}" / "${expr}" interpolation opens with an ARRAY token but
                            // closes with a RAW '}' string token. Without counting the opener,
                            // that '}' drove $bd to 0 and truncated the body scan at the first
                            // interpolated string — every param used after it got flagged unused.
                            $bd++;
                        } elseif ($bt[0] === T_VARIABLE) {
                            $used[$bt[1]] = true;
                        } elseif ($bt[0] === T_STRING && in_array($bt[1], ['func_get_args', 'compact', 'extract'], true)) {
                            $dynamic = true;            // dynamic arg access — can't trust name scan
                        }
                    }
                    if (!$dynamic) {
                        foreach ($paramVars as $pv) { if (!isset($used[$pv])) $unused[] = $pv; }
                    }
                }
            }

            echo json_encode([
                'file' => $path, 'line' => $line,
                'kind' => $class !== '' ? 'method' : 'function',
                'class' => $class, 'name' => $name, 'signature' => $sig,
                'doc' => $pendingDoc !== null,
                'doc_summary' => $pendingDoc !== null ? first_doc_line($pendingDoc) : '',
                'generated' => $pendingDoc !== null && strpos($pendingDoc, '@generated') !== false,
                'vis' => $class !== '' ? ($vis ?: 'public') : '',
                'unused_params' => $unused,
                'lang' => 'PHP',
            ], JSON_UNESCAPED_SLASHES) . "\n";
            $pendingDoc = null; $vis = ''; $prevSig = $id; continue;
        }

        // a docblock survives through modifiers/attributes (handled above); only the
        // structural `; { }` punctuation (handled in the is_string branch) drops it.
        $prevSig = $id;
    }
}

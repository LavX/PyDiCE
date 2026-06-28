// Ghidra Java script. Run with analyzeHeadless -postScript.
//@category pyobd

import ghidra.app.script.GhidraScript;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;

public class GhidraFindSymbols extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length == 0) {
            throw new IllegalArgumentException("usage: GhidraFindSymbols <needle> [<needle> ...]");
        }

        for (String needle : args) {
            println("===== SYMBOLS " + needle + " =====");
            SymbolIterator symbols = currentProgram.getSymbolTable().getAllSymbols(true);
            while (symbols.hasNext()) {
                Symbol symbol = symbols.next();
                String name = symbol.getName(true);
                if (name != null && name.contains(needle)) {
                    println(symbol.getAddress() + " " + name);
                }
            }
        }
    }
}

/**
 * Sample Java fixture for the multi-language repo.
 *
 * The Java equivalent of the Python shape: a class wraps the two
 * static methods so that `Main.target` and `Main.caller` are the
 * qualified names a blast test would search for.
 */
public class Main {
    static int target(int value) {
        return value * 2;
    }

    static int caller(int value) {
        return target(value) + 1;
    }
}
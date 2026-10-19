// Sample C# fixture for the multi-language repo.
//
// Same shape as `Main.java`: a class wraps the two static methods so
// that `Main.Target` and `Main.Caller` are the qualified names a
// blast test would search for.
public class Main
{
    static int Target(int value)
    {
        return value * 2;
    }

    static int Caller(int value)
    {
        return Target(value) + 1;
    }
}
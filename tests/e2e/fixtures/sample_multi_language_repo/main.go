// Sample Go fixture for the multi-language repo.
//
// Same shape as `main.py`: one `target` function and one `caller`
// function that calls it.
package main

func target(value int) int {
	return value * 2
}

func caller(value int) int {
	return target(value) + 1
}

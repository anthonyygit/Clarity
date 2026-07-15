import subprocess

def test_ocr(input_file, output_file):
    cmd = [
        "curl",
        "-X", "POST",
        "http://localhost:8000/ocr",
        "-F", f"image=@{input_file}",
        "--output", output_file
    ]

    print(f"\nTesting /ocr endpoint...")
    print(f"Input: {input_file}")
    print(f"Output: {output_file}")
    print()

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        print(f"✓ Success! WAV saved to {output_file}")
        print(f"You can play it with: afplay {output_file}")
    else:
        print(f"✗ Failed!")
        print(f"Error: {result.stderr}")
        return 1

    return 0

if __name__ == "__main__":
    input_file = input("Enter input image path: ")
    output_file = input("Enter output WAV path: ")

    test_ocr(input_file, output_file)

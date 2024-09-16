import os
import argparse
import glob
from functools import reduce
import matplotlib.pyplot as plt
from PIL import Image

def plot_png_grid(directories, file_names):
    # Define the grid size
    num_rows = len(file_names)
    num_cols = len(directories)

    # Create a figure and axes for the grid
    fig, axes = plt.subplots(num_rows, num_cols, figsize=(num_cols * 4, num_rows * 4))

    # Plot images in the grid
    for col, directory in enumerate(directories):
        for row, file_name in enumerate(file_names):
            file_path = os.path.join(directory, file_name)
            ax = axes[row, col] if num_rows > 1 and num_cols > 1 else (axes[col] if num_rows == 1 else axes[row])
            if os.path.isfile(file_path):
                # Load the image using PIL
                image = Image.open(file_path)
                # Display the image in the correct subplot
                ax.set_title(directory, fontsize=6)
                ax.imshow(image)
                ax.axis('off')  # Hide the axis
            else:
                # In case the image is not found, leave the subplot blank
                ax.set_visible(False)

    # Add column headers for directories
    for col, directory in enumerate(directories):
        fig.text(0.5 / num_cols + col / num_cols, 1.02, os.path.basename(directory), 
                 ha='center', va='bottom', fontsize=6, transform=fig.transFigure)

    # Add row headers for file names (reversing the order to match the top-down display)
    for row, file_name in enumerate(file_names):
        # Calculate the position based on the figure's coordinate system
        fig.text(-0.02, (num_rows - row - 0.5) / num_rows, file_name, 
                 ha='right', va='center', fontsize=6, transform=fig.transFigure)

    # Adjust layout to make room for headers
    plt.subplots_adjust(top=0.9, left=0.1, right=0.95, bottom=0.1, wspace=0.2, hspace=0.2)


    # Adjust layout to make room for headers
    plt.tight_layout(pad=2.0, w_pad=0.5, h_pad=1.0)
    plt.show()
    plt.savefig("grid.png", dpi=300, bbox_inches='tight')


def find_png_files(directories):
    retval = {}
    for d in directories:
        png_files = glob.glob(os.path.join(d, '*.png'))
        png_files = ['/'.join(file.split('/')[1:]) for file in png_files]
        if len(png_files) == 0:
            print(f"{d} contains no .png files")
            exit(1)
        retval[d] = png_files
    sets = [set(lst) for lst in retval.values()]
    intersection = list(reduce(set.intersection, sets))
    intersection.sort(key=lambda x: x.split('_')[1])
    return intersection

def main():
    parser = argparse.ArgumentParser(description='Create a grid from the latency values')
    parser.add_argument('directories', nargs='+', type=str, help='The directory to search png files.')
    args = parser.parse_args()

    for directory in args.directories:
        if not os.path.isdir(directory):
            print(f"{directory} is not a valid directory.")
            exit(1)
    
    files = find_png_files(args.directories)
    print(files)

    plot_png_grid(args.directories, files)

if __name__ == '__main__':
    main()

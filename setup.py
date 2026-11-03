from setuptools import setup, find_packages

setup(
    name='autoply',
    version='1.0.1',
    description='A local-first AI job application workflow assistant.',
    long_description=open('README.md', encoding='utf-8').read(),
    long_description_content_type='text/markdown',
    author='Ashmit Gautam',
    author_email='ashmitgautam788@gmail.com',
    license='MIT',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Developers',
        'Topic :: Software Development :: Build Tools',
        'Programming Language :: Python :: 3.11',
    ],
    packages=find_packages(),
    python_requires='>=3.11',  # Updated version specifier
    install_requires=[
        'selenium',
        'tk',
        'python-dotenv',
        'google-genai',
        'webdriver_manager',
        'PyPDF2',
        'openpyxl',
        'fastapi',
        'uvicorn[standard]',
        'jinja2',
        'python-multipart',
    ],
    setup_requires=['wheel'],
)
